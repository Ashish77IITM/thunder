"""Provides ImagesLoader object and helpers, used to read Images data from disk or other filesystems.
"""
from matplotlib.pyplot import imread
from io import BytesIO
from numpy import array, dstack, frombuffer, ndarray, prod
from thunder.rdds.fileio.readers import getParallelReaderForPath
from thunder.rdds.images import Images


class ImagesLoader(object):
    """Loader object used to instantiate Images data stored in a variety of formats.
    """
    def __init__(self, sparkContext):
        """Initialize a new ImagesLoader object.

        Parameters
        ----------
        sparkcontext: SparkContext
            The pyspark SparkContext object used by the current Thunder environment.
        """
        self.sc = sparkContext

    def fromArrays(self, arrays):
        """Load Images data from passed sequence of numpy arrays.

        Expected usage is mainly in testing - having a full dataset loaded in memory
        on the driver is likely prohibitive in the use cases for which Thunder is intended.
        """
        # if passed a single array, cast it to a sequence of length 1
        if isinstance(arrays, ndarray):
            arrays = [arrays]

        shape = None
        dtype = None
        for ary in arrays:
            if shape is None:
                shape = ary.shape
                dtype = ary.dtype
            if not ary.shape == shape:
                raise ValueError("Arrays must all be of same shape; got both %s and %s" %
                                 (str(shape), str(ary.shape)))
            if not ary.dtype == dtype:
                raise ValueError("Arrays must all be of same data type; got both %s and %s" %
                                 (str(dtype), str(ary.dtype)))
        return Images(self.sc.parallelize(enumerate(arrays), len(arrays)),
                      dims=shape, dtype=str(dtype), nimages=len(arrays))

    def fromStack(self, dataPath, dims, dtype='int16', ext='stack', startIdx=None, stopIdx=None, recursive=False):
        """Load an Images object stored in a directory of flat binary files

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Currently all binary data read by this method is assumed to be formatted as signed 16 bit integers in native
        byte order.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        dims: tuple of positive int
            Dimensions of input image data, ordered with fastest-changing dimension first

        ext: string, optional, default "stack"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).
        """
        if not dims:
            raise ValueError("Image dimensions must be specified if loading from binary stack data")

        def toArray(buf):
            return frombuffer(buf, dtype=dtype, count=int(prod(dims))).reshape(dims, order='F')

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        return Images(readerRdd.mapValues(toArray), nimages=reader.lastNRecs, dims=dims,
                      dtype=dtype)

    def fromTif(self, dataPath, ext='tif', startIdx=None, stopIdx=None, recursive=False):
        """Load an Images object stored in a directory of (single-page) tif files

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        ext: string, optional, default "tif"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).
        """
        def readTifFromBuf(buf):
            fbuf = BytesIO(buf)
            return imread(fbuf, format='tif')

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        return Images(readerRdd.mapValues(readTifFromBuf), nimages=reader.lastNRecs)

    def fromMultipageTif(self, dataPath, ext='tif', startIdx=None, stopIdx=None, recursive=False, nplanes=None):
        """Sets up a new Images object with data to be read from one or more multi-page tif files.

        The RDD underlying the returned Images will have key, value data as follows:

        key: int or (int, int)
            key is index of original data file, determined by lexicographic ordering of filenames.
            If nplanes is passed, then the key will be an integer pair (index of original data file, timepoint within file)
        value: numpy ndarray
            value dimensions with be x by y by num_channels*num_pages; all channels and pages in a file are
            concatenated together in the third dimension of the resulting ndarray. For pages 0, 1, etc
            of a multipage TIF of RGB images, ary[:,:,0] will be R channel of page 0 ("R0"), ary[:,:,1] will be B0,
            ... ary[:,:,3] == R1, and so on.

        This method attempts to explicitly import PIL. ImportError may be thrown if 'from PIL import Image' is
        unsuccessful. (PIL/pillow is not an explicit requirement for thunder.)
        """
        try:
            from PIL import Image
        except ImportError, e:
            Image = None
            raise ImportError("fromMultipageTif requires a successful 'from PIL import Image'; " +
                              "the PIL/pillow library appears to be missing or broken.", e)
        # we know that that array(pilimg) works correctly for pillow == 2.3.0, and that it
        # does not work (at least not with spark) for old PIL == 1.1.7. we believe but have not confirmed
        # that array(pilimg) works correctly for every version of pillow. thus currently we check only whether
        # our PIL library is in fact pillow, and choose our conversion function accordingly
        isPillow = hasattr(Image, "PILLOW_VERSION")
        if isPillow:
            conversionFcn = array  # use numpy's array() function
        else:
            from thunder.utils.common import pil_to_array
            conversionFcn = pil_to_array  # use our modified version of matplotlib's pil_to_array

        if nplanes is not None and nplanes <= 0:
            raise ValueError("nplanes must be positive if passed, got %d" % nplanes)

        def multitifReader(idxAndBuf):
            idx, buf = idxAndBuf
            fbuf = BytesIO(buf)
            multipage = Image.open(fbuf)
            pageIdx = 0
            imgArys = []
            npagesLeft = -1 if nplanes is None else nplanes  # counts number of planes remaining in image if positive
            timepoints = 0  # counts number of images generated from this file
            while True:
                try:
                    multipage.seek(pageIdx)
                    imgArys.append(conversionFcn(multipage))
                    pageIdx += 1
                    npagesLeft -= 1
                    if npagesLeft == 0:
                        # we have just finished an image from this file
                        retAry = dstack(imgArys) if len(imgArys) > 1 else imgArys[0]
                        yield (idx, timepoints), retAry
                        # reset counters:
                        timepoints += 1
                        npagesLeft = nplanes
                        imgArys = []
                except EOFError:
                    # past last page in tif
                    break
            if imgArys:
                retAry = dstack(imgArys) if len(imgArys) > 1 else imgArys[0]
                # key should be (idx, timepoints) if we have passed nplanes, else just idx
                retKey = (idx, timepoints) if npagesLeft >= 0 else idx
                yield retKey, retAry

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        return Images(readerRdd.flatMap(multitifReader), nimages=reader.lastNRecs)

    def fromPng(self, dataPath, ext='png', startIdx=None, stopIdx=None, recursive=False):
        """Load an Images object stored in a directory of png files

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        ext: string, optional, default "png"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).
        """
        def readPngFromBuf(buf):
            fbuf = BytesIO(buf)
            return imread(fbuf, format='png')

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        return Images(readerRdd.mapValues(readPngFromBuf), nimages=reader.lastNRecs)
