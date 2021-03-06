# coding=utf-8
"""
Create/store dataset data into storage units based on the provided storage mappings
"""
from __future__ import absolute_import, division, print_function

import logging
import math
from contextlib import contextmanager
from pathlib import Path

from datacube.compat import urlparse, urljoin, url_parse_module
from datacube.config import OPTIONS
from datacube.model import Dataset
from datacube.storage import netcdf_writer
from datacube.utils import clamp, data_resolution_and_offset, datetime_to_seconds_since_1970, DatacubeException
from datacube.utils import geometry
from datacube.utils import is_url, uri_to_local_path

try:
    from yaml import CSafeDumper as SafeDumper
except ImportError:
    from yaml import SafeDumper
import numpy

from affine import Affine
from datacube.compat import integer_types
import rasterio.warp
import rasterio.crs

try:
    from rasterio.warp import Resampling
except ImportError:
    from rasterio.warp import RESAMPLING as Resampling

_LOG = logging.getLogger(__name__)

RESAMPLING_METHODS = {
    'nearest': Resampling.nearest,
    'cubic': Resampling.cubic,
    'bilinear': Resampling.bilinear,
    'cubic_spline': Resampling.cubic_spline,
    'lanczos': Resampling.lanczos,
    'average': Resampling.average,
}

assert str(rasterio.__version__) >= '0.34.0', "rasterio version 0.34.0 or higher is required"
GDAL_NETCDF_DIM = ('NETCDF_DIM_'
                   if str(rasterio.__gdal_version__) >= '1.10.0' else
                   'NETCDF_DIMENSION_')


def _rasterio_resampling_method(resampling):
    return RESAMPLING_METHODS[resampling.lower()]


if str(rasterio.__version__) >= '0.36.0':
    def _rasterio_crs_wkt(src):
        return str(src.crs.wkt)
else:
    def _rasterio_crs_wkt(src):
        return str(src.crs_wkt)

if str(rasterio.__version__) >= '1.0':
    def _rasterio_transform(src):
        return src.transform
else:
    def _rasterio_transform(src):
        return src.affine


def _calc_offsets_impl(off, scale, src_size, dst_size):
    assert scale >= 1-1e-5

    if off >= 0:
        write_off = 0
    else:
        write_off = math.ceil((-off-0.5)/scale)
    read_off = round((write_off+0.5)*scale-0.5+off) - round(0.5*(scale-1.0))  # assuming read_size/write_size ~= scale
    if read_off >= src_size:
        return 0, 0, 0, 0

    write_end = dst_size
    write_size = write_end-write_off
    read_end = read_off+round(write_size*scale)
    if read_end > src_size:
        # +0.5 below is a fudge that will return last row in more situations, but will change the scale more
        write_end = math.floor((src_size-off+0.5)/scale)
        write_size = write_end-write_off
        read_end = clamp(read_off+round(write_size*scale), read_off, src_size)
    read_size = read_end-read_off

    return int(read_off), int(write_off), int(read_size), int(write_size)


def _calc_offsets2(off, scale, src_size, dst_size):
    if scale < 0:
        r_off, write_off, read_size, write_size = _calc_offsets_impl(off + dst_size*scale, -scale, src_size, dst_size)
        return r_off, dst_size - write_size - write_off, read_size, write_size
    else:
        return _calc_offsets_impl(off, scale, src_size, dst_size)


def _read_decimated(array_transform, src, dest_shape):
    dy_dx = (array_transform.f, array_transform.c)
    sy_sx = (array_transform.e, array_transform.a)
    read, write, read_shape, write_shape = zip(*map(_calc_offsets2, dy_dx, sy_sx, src.shape, dest_shape))
    if all(write_shape):
        window = ((read[0], read[0] + read_shape[0]), (read[1], read[1] + read_shape[1]))
        tmp = src.read(window=window, out_shape=write_shape)
        scale = (read_shape[0]/write_shape[0] if sy_sx[0] > 0 else -read_shape[0]/write_shape[0],
                 read_shape[1]/write_shape[1] if sy_sx[1] > 0 else -read_shape[1]/write_shape[1])
        offset = (read[0] + (0 if sy_sx[0] > 0 else read_shape[0]),
                  read[1] + (0 if sy_sx[1] > 0 else read_shape[1]))
        transform = Affine(scale[1], 0, offset[1], 0, scale[0], offset[0])
        return tmp[::(-1 if sy_sx[0] < 0 else 1), ::(-1 if sy_sx[1] < 0 else 1)], write, transform
    return None, None, None


def _no_scale(affine, eps=1e-5):
    return abs(abs(affine.a) - 1.0) < eps and abs(abs(affine.e) - 1.0) < eps


def _no_fractional_translate(affine, eps=0.01):
    return abs(affine.c % 1.0) < eps and abs(affine.f % 1.0) < eps


def read_from_source(source, dest, dst_transform, dst_nodata, dst_projection, resampling):
    """
    Read from `source` into `dest`, reprojecting if necessary.

    :param BaseRasterDataSource source: Data source
    :param numpy.ndarray dest: Data destination
    """
    with source.open() as src:
        array_transform = ~src.transform * dst_transform
        # if the CRS is the same use decimated reads if possible (NN or 1:1 scaling)
        if src.crs == dst_projection and _no_scale(array_transform) and (resampling == Resampling.nearest or
                                                                         _no_fractional_translate(array_transform)):
            dest.fill(dst_nodata)
            tmp, offset, _ = _read_decimated(array_transform, src, dest.shape)
            if tmp is None:
                return
            dest = dest[offset[0]:offset[0] + tmp.shape[0], offset[1]:offset[1] + tmp.shape[1]]
            numpy.copyto(dest, tmp, where=(tmp != src.nodata))
        else:
            if dest.dtype == numpy.dtype('int8'):
                dest = dest.view(dtype='uint8')
                dst_nodata = dst_nodata.astype('uint8')
            src.reproject(dest,
                          dst_transform=dst_transform,
                          dst_crs=str(dst_projection),
                          dst_nodata=dst_nodata,
                          resampling=resampling,
                          NUM_THREADS=OPTIONS['reproject_threads'])


@contextmanager
def ignore_exceptions_if(ignore_errors):
    """Ignore Exceptions raised within this block if ignore_errors is True"""
    if ignore_errors:
        try:
            yield
        except OSError as e:
            _LOG.warning('Ignoring Exception: %s', e)
    else:
        yield


def reproject_and_fuse(sources, destination, dst_transform, dst_projection, dst_nodata,
                       resampling='nearest', fuse_func=None, skip_broken_datasets=False):
    """
    Reproject and fuse `sources` into a 2D numpy array `destination`.

    :param List[BaseRasterDataSource] sources: Data sources to open and read from
    :param numpy.ndarray destination: ndarray of appropriate size to read data into
    :type resampling: str
    :type fuse_func: callable or None
    :param bool skip_broken_datasets: Carry on in the face of adversity and failing reads.
    """
    assert len(destination.shape) == 2

    resampling = _rasterio_resampling_method(resampling)

    def copyto_fuser(dest, src):
        """
        :type dest: numpy.ndarray
        :type src: numpy.ndarray
        """
        numpy.copyto(dest, src, where=(dest == dst_nodata))

    fuse_func = fuse_func or copyto_fuser

    destination.fill(dst_nodata)
    if len(sources) == 0:
        return destination
    elif len(sources) == 1:
        with ignore_exceptions_if(skip_broken_datasets):
            read_from_source(sources[0], destination, dst_transform, dst_nodata, dst_projection, resampling)
        return destination
    else:
        # Muitiple sources, we need to fuse them together into a single array
        buffer_ = numpy.empty(destination.shape, dtype=destination.dtype)
        for source in sources:
            with ignore_exceptions_if(skip_broken_datasets):
                read_from_source(source, buffer_, dst_transform, dst_nodata, dst_projection, resampling)
                fuse_func(destination, buffer_)

        return destination


class BandDataSource(object):
    """Wrapper for a rasterio.Band object

    :param source: rasterio.Band
    """
    def __init__(self, source, nodata=None):
        self.source = source
        if nodata is None:
            assert self.source.ds.nodatavals[0] is not None
            nodata = self.dtype.type(self.source.ds.nodatavals[0])
        self.nodata = nodata

    @property
    def crs(self):
        return geometry.CRS(_rasterio_crs_wkt(self.source.ds))

    @property
    def transform(self):
        return _rasterio_transform(self.source.ds)

    @property
    def dtype(self):
        return numpy.dtype(self.source.dtype)

    @property
    def shape(self):
        return self.source.shape

    def read(self, window=None, out_shape=None):
        """Read data in the native format, returning a native array
        """
        return self.source.ds.read(indexes=self.source.bidx, window=window, out_shape=out_shape)

    def reproject(self, dest, dst_transform, dst_crs, dst_nodata, resampling, **kwargs):
        return rasterio.warp.reproject(self.source,
                                       dest,
                                       src_nodata=self.nodata,
                                       dst_transform=dst_transform,
                                       dst_crs=str(dst_crs),
                                       dst_nodata=dst_nodata,
                                       resampling=resampling,
                                       **kwargs)


class NetCDFDataSource(object):
    def __init__(self, dataset, variable, slab=None, nodata=None):
        self.dataset = dataset
        self.variable = self.dataset[variable]
        self.slab = slab or {}
        if nodata is None:
            nodata = self.variable.getncattr('_FillValue')
        self.nodata = nodata

    @property
    def crs(self):
        crs_var_name = self.variable.grid_mapping
        crs_var = self.dataset[crs_var_name]
        return geometry.CRS(crs_var.crs_wkt)

    @property
    def transform(self):
        dims = self.crs.dimensions
        xres, xoff = data_resolution_and_offset(self.dataset[dims[1]])
        yres, yoff = data_resolution_and_offset(self.dataset[dims[0]])
        return Affine.translation(xoff, yoff) * Affine.scale(xres, yres)

    @property
    def dtype(self):
        return self.variable.dtype

    @property
    def shape(self):
        return self.variable.shape

    def read(self, window=None, out_shape=None):
        data = self.variable
        if window is None:
            window = ((0, data.shape[0]), (0, data.shape[1]))
        data_shape = (window[0][1]-window[0][0]), (window[1][1]-window[1][0])
        if out_shape is None:
            out_shape = data_shape
        xidx = window[0][0] + ((numpy.arange(out_shape[1])+0.5)*(data_shape[1]/out_shape[1])-0.5).round().astype('int')
        yidx = window[1][0] + ((numpy.arange(out_shape[0])+0.5)*(data_shape[0]/out_shape[0])-0.5).round().astype('int')
        slab = {self.crs.dimensions[1]: xidx, self.crs.dimensions[0]: yidx}
        slab.update(self.slab)
        return data[tuple(slab[d] for d in self.variable.dimensions)]

    def reproject(self, dest, dst_transform, dst_crs, dst_nodata, resampling, **kwargs):
        dst_poly = geometry.polygon_from_transform(dest.shape[1], dest.shape[0],
                                                   dst_transform, dst_crs).to_crs(self.crs)
        src_poly = geometry.polygon_from_transform(self.shape[1], self.shape[0],
                                                   self.transform, self.crs)
        bounds = dst_poly.intersection(src_poly)
        geobox = geometry.GeoBox.from_geopolygon(bounds, (self.transform.e, self.transform.a), crs=self.crs)
        tmp, _, tmp_transform = _read_decimated(~self.transform * geobox.affine, self, geobox.shape)

        return rasterio.warp.reproject(tmp,
                                       dest,
                                       src_transform=self.transform * tmp_transform,
                                       src_crs=str(geobox.crs),
                                       src_nodata=self.nodata,
                                       dst_transform=dst_transform,
                                       dst_crs=str(dst_crs),
                                       dst_nodata=dst_nodata,
                                       resampling=resampling,
                                       **kwargs)


class OverrideBandDataSource(object):
    """Wrapper for a rasterio.Band object that overrides nodata, crs and transform

    This is useful for files with malformed or missing properties

    :param source: rasterio.Band
    """
    def __init__(self, source, nodata, crs, transform):
        self.source = source
        self.nodata = nodata
        self.crs = crs
        self.transform = transform

    @property
    def dtype(self):
        return numpy.dtype(self.source.dtype)

    @property
    def shape(self):
        return self.source.shape

    def read(self, window=None, out_shape=None):
        """Read data in the native format, returning a native array
        """
        return self.source.ds.read(indexes=self.source.bidx, window=window, out_shape=out_shape)

    def reproject(self, dest, dst_transform, dst_crs, dst_nodata, resampling, **kwargs):
        source = self.read()  # TODO: read only the part the we care about
        return rasterio.warp.reproject(source,
                                       dest,
                                       src_transform=self.transform,
                                       src_crs=str(self.crs),
                                       src_nodata=self.nodata,
                                       dst_transform=dst_transform,
                                       dst_crs=str(dst_crs),
                                       dst_nodata=dst_nodata,
                                       resampling=resampling,
                                       **kwargs)


class BaseRasterDataSource(object):
    """
    Interface used by fuse_sources and read_from_source
    """

    def __init__(self, filename, nodata):
        self.filename = filename
        self.nodata = nodata

    def get_bandnumber(self, src):
        raise NotImplementedError()

    def get_transform(self, shape):
        raise NotImplementedError()

    def get_crs(self):
        raise NotImplementedError()

    @contextmanager
    def open(self):
        """Context manager which returns a `BandDataSource`"""
        try:
            _LOG.debug("opening %s", self.filename)
            with rasterio.open(self.filename) as src:
                override = False

                transform = _rasterio_transform(src)
                if transform.is_identity:
                    override = True
                    transform = self.get_transform(src.shape)

                try:
                    crs = geometry.CRS(_rasterio_crs_wkt(src))
                except ValueError:
                    override = True
                    crs = self.get_crs()

                bandnumber = self.get_bandnumber(src)
                band = rasterio.band(src, bandnumber)
                nodata = numpy.dtype(band.dtype).type(src.nodatavals[0] if src.nodatavals[0] is not None
                                                      else self.nodata)

                if override:
                    yield OverrideBandDataSource(band, nodata=nodata, crs=crs, transform=transform)
                else:
                    yield BandDataSource(band, nodata=nodata)

        except Exception as e:
            _LOG.error("Error opening source dataset: %s", self.filename)
            raise e


class RasterFileDataSource(BaseRasterDataSource):
    def __init__(self, filename, bandnumber, nodata=None, crs=None, transform=None):
        super(RasterFileDataSource, self).__init__(filename, nodata)
        self.bandnumber = bandnumber
        self.crs = crs
        self.transform = transform

    def get_bandnumber(self, src):
        return self.bandnumber

    def get_transform(self, shape):
        if self.transform is None:
            raise RuntimeError('No transform in the data and no fallback')
        return self.transform

    def get_crs(self):
        if self.crs is None:
            raise RuntimeError('No CRS in the data and no fallback')
        return self.crs


def register_scheme(*schemes):
    """
    Register additional uri schemes as supporting relative offsets (etc), so that band/measurement paths can be
    calculated relative to the base uri.
    """
    url_parse_module.uses_netloc.extend(schemes)
    url_parse_module.uses_relative.extend(schemes)
    url_parse_module.uses_params.extend(schemes)

# Not recognised by python by default. Doctests below will fail without it.
register_scheme('s3')


def _resolve_url(base_url, path):
    """
    If path is a URL or an absolute path return URL
    If path is a relative path return base_url joined with path

    >>> _resolve_url('file:///foo/abc', 'bar')
    'file:///foo/bar'
    >>> _resolve_url('file:///foo/abc', 'file:///bar')
    'file:///bar'
    >>> _resolve_url('file:///foo/abc', None)
    'file:///foo/abc'
    >>> _resolve_url('file:///foo/abc', '/bar')
    'file:///bar'
    >>> _resolve_url('http://foo.com/abc/odc-metadata.yaml', 'band-5.tif')
    'http://foo.com/abc/band-5.tif'
    >>> _resolve_url('s3://foo.com/abc/odc-metadata.yaml', 'band-5.tif')
    's3://foo.com/abc/band-5.tif'
    >>> _resolve_url('s3://foo.com/abc/odc-metadata.yaml?something', 'band-5.tif')
    's3://foo.com/abc/band-5.tif'
    """
    if path:
        if is_url(path):
            url_str = path
        elif Path(path).is_absolute():
            url_str = Path(path).as_uri()
        else:
            url_str = urljoin(base_url, path)
    else:
        url_str = base_url
    return url_str


def _url2rasterio(url_str, fmt, layer):
    """
    turn URL into a string that could be passed to raterio.open
    """
    url = urlparse(url_str)
    assert url.scheme, "Expecting URL with scheme here"

    # if format is NETCDF of HDF need to pass NETCDF:path:band as filename to rasterio/GDAL
    for nasty_format in ('netcdf', 'hdf'):
        if nasty_format in fmt.lower():
            if url.scheme != 'file':
                raise RuntimeError("Can't access %s over %s" % (fmt, url.scheme))
            filename = '%s:%s:%s' % (fmt, uri_to_local_path(url_str), layer)
            return filename

    if url.scheme and url.scheme != 'file':
        return url_str

    # if local path strip scheme and other gunk
    return str(uri_to_local_path(url_str))


def _choose_location(dataset):
    # type: (Dataset) -> str

    # If there's a local (filesystem) URI, prefer it.
    local_uri = dataset.local_uri
    if local_uri:
        return local_uri

    uris = dataset.uris
    if not uris:
        # Location-less datasets should have been filtered already.
        raise RuntimeError("No recorded location for dataset {}".format(dataset))

    # Newest location first, use it.
    # We may want more nuanced selection in the future.
    return uris[0]


class DatasetSource(BaseRasterDataSource):
    """Data source for reading from a Datacube Dataset"""

    def __init__(self, dataset, measurement_id):
        self._dataset = dataset
        self._measurement = dataset.measurements[measurement_id]
        url = _resolve_url(_choose_location(dataset), self._measurement['path'])
        filename = _url2rasterio(url, dataset.format, self._measurement.get('layer'))
        nodata = dataset.type.measurements[measurement_id].get('nodata')
        super(DatasetSource, self).__init__(filename, nodata=nodata)

    def get_bandnumber(self, src):
        if 'netcdf' not in self._dataset.format.lower():
            layer_id = self._measurement.get('layer', 1)
            return layer_id if isinstance(layer_id, integer_types) else 1

        tag_name = GDAL_NETCDF_DIM + 'time'
        if tag_name not in src.tags(1):  # TODO: support time-less datasets properly
            return 1

        time = self._dataset.center_time
        sec_since_1970 = datetime_to_seconds_since_1970(time)

        idx = 0
        dist = float('+inf')
        for i in range(1, src.count + 1):
            v = float(src.tags(i)[tag_name])
            if abs(sec_since_1970 - v) < dist:
                idx = i
                dist = abs(sec_since_1970 - v)
        return idx

    def get_transform(self, shape):
        return self._dataset.transform * Affine.scale(1/shape[1], 1/shape[0])

    def get_crs(self):
        return self._dataset.crs


def create_netcdf_storage_unit(filename,
                               crs, coordinates, variables, variable_params, global_attributes=None,
                               netcdfparams=None):
    """
    Create a NetCDF file on disk.

    :param pathlib.Path filename: filename to write to
    :param datacube.utils.geometry.CRS crs: Datacube CRS object defining the spatial projection
    :param dict coordinates: Dict of named `datacube.model.Coordinate`s to create
    :param dict variables: Dict of named `datacube.model.Variable`s to create
    :param dict variable_params:
        Dict of dicts, with keys matching variable names, of extra parameters for variables
    :param dict global_attributes: named global attributes to add to output file
    :param dict netcdfparams: Extra parameters to use when creating netcdf file
    :return: open netCDF4.Dataset object, ready for writing to
    """
    filename = Path(filename)
    if filename.exists():
        raise RuntimeError('Storage Unit already exists: %s' % filename)

    try:
        filename.parent.mkdir(parents=True)
    except OSError:
        pass

    _LOG.info('Creating storage unit: %s', filename)

    nco = netcdf_writer.create_netcdf(str(filename), **(netcdfparams or {}))

    for name, coord in coordinates.items():
        netcdf_writer.create_coordinate(nco, name, coord.values, coord.units)

    netcdf_writer.create_grid_mapping_variable(nco, crs)

    for name, variable in variables.items():
        set_crs = all(dim in variable.dims for dim in crs.dimensions)
        var_params = variable_params.get(name, {})
        data_var = netcdf_writer.create_variable(nco, name, variable, set_crs=set_crs, **var_params)

        for key, value in var_params.get('attrs', {}).items():
            setattr(data_var, key, value)

    for key, value in (global_attributes or {}).items():
        setattr(nco, key, value)

    return nco


def write_dataset_to_netcdf(dataset, filename, global_attributes=None, variable_params=None,
                            netcdfparams=None):
    """
    Write a Data Cube style xarray Dataset to a NetCDF file

    Requires a spatial Dataset, with attached coordinates and global crs attribute.

    :param `xarray.Dataset` dataset:
    :param filename: Output filename
    :param global_attributes: Global file attributes. dict of attr_name: attr_value
    :param variable_params: dict of variable_name: {param_name: param_value, [...]}
                            Allows setting storage and compression options per variable.
                            See the `netCDF4.Dataset.createVariable` for available
                            parameters.
    :param netcdfparams: Optional params affecting netCDF file creation
    """
    global_attributes = global_attributes or {}
    variable_params = variable_params or {}
    filename = Path(filename)

    if not dataset.data_vars.keys():
        raise DatacubeException('Cannot save empty dataset to disk.')

    if not hasattr(dataset, 'crs'):
        raise DatacubeException('Dataset does not contain CRS, cannot write to NetCDF file.')

    nco = create_netcdf_storage_unit(filename,
                                     dataset.crs,
                                     dataset.coords,
                                     dataset.data_vars,
                                     variable_params,
                                     global_attributes,
                                     netcdfparams)

    for name, variable in dataset.data_vars.items():
        nco[name][:] = netcdf_writer.netcdfy_data(variable.values)

    nco.close()
