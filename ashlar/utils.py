import itertools
import warnings
import pyfftw
import skimage
import scipy
import numpy as np


def fft2(img):
    return pyfftw.builders.fft2(img, planner_effort='FFTW_ESTIMATE',
                                avoid_copy=True, auto_align_input=True,
                                auto_contiguous=True)()


# Pre-calculate the Laplacian operator kernel. We'll always be using 2D images.
_laplace_kernel = skimage.restoration.uft.laplacian(2, (3, 3))[1]

def whiten(img, sigma):
    # Copied from skimage.filters.edges, with explicit aligned output from
    # convolve. Also the mask option was dropped.
    img = skimage.img_as_float(img)
    output = pyfftw.empty_aligned(img.shape, 'complex64')
    output.imag[:] = 0
    if sigma == 0:
        scipy.ndimage.convolve(img, _laplace_kernel, output.real)
    else:
        scipy.ndimage.gaussian_laplace(img, sigma, output=output.real)
    return output

    # Other possible whitening functions:
    #img = skimage.filters.roberts(img)
    #img = skimage.filters.scharr(img)
    #img = skimage.filters.sobel(img)
    #img = np.log(img)
    #img = img - scipy.ndimage.filters.gaussian_filter(img, 2) + 0.5


def register(img1, img2, sigma):
    img1w = whiten(img1, sigma)
    img2w = whiten(img2, sigma)
    img1_f = fft2(img1w)
    img2_f = fft2(img2w)
    img1w = img1w.real
    img2w = img2w.real
    shift, _, _ = skimage.feature.register_translation(
        img1_f, img2_f, 10, 'fourier'
    )
    # At this point we may have a shift in the wrong quadrant since the FFT
    # assumes the signal is periodic. We test all four possibilities and return
    # the shift that gives the highest direct correlation (sum of products).
    shape = np.array(img1.shape)
    shift_pos = (shift + shape) % shape
    shift_neg = shift_pos - shape
    shifts = list(itertools.product(*zip(shift_pos, shift_neg)))
    correlations = [
        np.abs(np.sum(img1w * scipy.ndimage.shift(img2w, s)))
        for s in shifts
    ]
    idx = np.argmax(correlations)
    shift = shifts[idx]
    correlation = correlations[idx]
    total_amplitude = np.linalg.norm(img1w) * np.linalg.norm(img2w)
    if correlation > 0 and total_amplitude > 0:
        error = -np.log(correlation / total_amplitude)
    else:
        error = np.inf
    return shift, error


def crop(img, offset, shape):
    # Note that this only crops to the nearest whole-pixel offset.
    start = offset.astype(int)
    end = start + shape
    img = img[start[0]:end[0], start[1]:end[1]]
    return img


# TODO:
# - Deal with ringing from high-frequency elements. The wrapped edges of the
#   image are especially bad, where the wrapping introduces sharp
#   discontinuities. The edge artifacts could be dealt with in several ways
#   (extend the trailing image edge via mirroring, throw away some of the
#   trailing edge of the shifted result) but edges in the "true" image content
#   would require proper pre-filtering. What filter to use, and how to apply it
#   quickly?
# - Can we use real FFT for a ~50% overall speedup? Fourier-space matrices will
#   all be half-size in the last dimension, so FFT is around 50% faster and our
#   fshift calculations will be too.
# - Trailing edge pixels should be zeroed to match the behavior of
#   scipy.ndimage.shift, which we rely on in our maximum-intensity projection.
def fourier_shift(img, shift):
    # Ensure properly aligned complex64 data (fft requires complex to avoid
    # reallocation and copying).
    img = convert(img, np.float32)
    img = pyfftw.byte_align(img, dtype=np.complex64)
    # Compute per-axis frequency values according to the Fourier shift theorem.
    # (Read "w" here as "omega".) We pre-multiply as many scalar values as
    # possible on these vectors to avoid operations on the full w matrix below.
    v = np.fft.fftfreq(img.shape[0])
    wy = (2 * np.pi * v * shift[0]).astype(np.float32).reshape(-1, 1)
    u = np.fft.fftfreq(img.shape[1])
    wx = (2 * np.pi * u * shift[1]).astype(np.float32)
    # Add column and row vector to get full expanded matrix of frequencies.
    w = wy + wx
    # We perform an explicit application of Euler's formula with careful
    # management of output arrays to avoid extra memory allocations and copies,
    # squeezing out some speed over the obvious np.exp(-1j*w).
    fshift = np.empty_like(img, dtype=np.complex64)
    np.cos(w, out=fshift.real)
    np.sin(w, out=fshift.imag)
    np.negative(fshift.imag, out=fshift.imag)
    # Perform the FFT, multiply in-place by the shift matrix, then IFFT.
    freq = pyfftw.builders.fft2(img, planner_effort='FFTW_ESTIMATE',
                                avoid_copy=True, auto_align_input=True,
                                auto_contiguous=True)()
    freq *= fshift
    img_s = pyfftw.builders.ifft2(freq, planner_effort='FFTW_ESTIMATE',
                                  avoid_copy=True, auto_align_input=True,
                                  auto_contiguous=True)()
    # Any non-zero imaginary component of the resulting array is due to
    # numerical error, so we can just return the real part.
    # FIXME need to zero out row(s) and column(s) we shifted away from,
    # since at this point we have a cyclic rotation rather than a shift.
    return img_s.real


def paste(target, img, pos, func=None):
    """Composite img into target."""
    pos = np.array(pos)
    # Bail out if destination region is out of bounds.
    if np.any(pos >= target.shape[:2]) or np.any(pos + img.shape[:2] < 0):
        return
    pos_f, pos_i = np.modf(pos)
    yi, xi = pos_i.astype('i8')
    # Clip img to the edges of the mosaic.
    if yi < 0:
        img = img[-yi:]
        yi = 0
    if xi < 0:
        img = img[:, -xi:]
        xi = 0
    target_slice = target[yi:yi+img.shape[0], xi:xi+img.shape[1]]
    img = crop_like(img, target_slice)
    if img.ndim == 2:
        img = scipy.ndimage.shift(img, pos_f)
    else:
        for c in range(img.shape[2]):
            img[...,c] = scipy.ndimage.shift(img[...,c], pos_f)
    # For any axis where there is a non-zero subpixel shift, crop out the last
    # row or column of pixels on the "losing" side. These pixels will be darker
    # than normal and will introduce artifacts in most blending modes.
    y1 = None if pos_f[0] <= 0 else 1
    y2 = None if pos_f[0] >= 0 else -1
    x1 = None if pos_f[1] <= 0 else 1
    x2 = None if pos_f[1] >= 0 else -1
    img = img[y1:y2, x1:x2]
    target_slice = target_slice[y1:y2, x1:x2]
    if np.issubdtype(img.dtype, np.floating):
        np.clip(img, 0, 1, img)
    img = convert(img, target.dtype)
    if func is None:
        target_slice[:] = img
    elif isinstance(func, np.ufunc):
        func(target_slice, img, out=target_slice)
    else:
        target_slice[:] = func(target_slice, img)


def pastefunc_blend(target, img):
    # Linear blend based on distance to unfilled space in target.
    dist = scipy.ndimage.distance_transform_cdt(target)
    dmax = dist.max()
    if dmax == 0:
        alpha = 0
    else:
        alpha = dist / dist.max()
    return target * alpha + img * (1 - alpha)


def crop_like(img, target):
    if (img.shape[0] > target.shape[0]):
        img = img[:target.shape[0], :]
    if (img.shape[1] > target.shape[1]):
        img = img[:, :target.shape[1]]
    return img


def convert(image, dtype, **kwargs):
    """
    Convert an image to the requested data-type.

    This is just a wrapper around skimage's convert function to suppress the
    precision loss warning.

    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore', 'Possible precision loss', UserWarning,
            '^skimage\.util\.dtype'
        )
        return skimage.util.dtype.convert(image, dtype, **kwargs)


def imsave(fname, arr, **kwargs):
    """Save an image to file.

    This is a wrapper around skimage.io.imsave to handle the lack of the
    check_contrast argument on v0.14, which is the last version to support
    Python 2.7.

    """
    if skimage.__version__.startswith('0.14.'):
        warnings.warn(
            "Please upgrade to Python 3 and scikit-image v0.15+ to make image"
            " saving much less resource-intensive."
        )
    else:
        kwargs['check_contrast'] = False
    return skimage.io.imsave(fname, arr, **kwargs)