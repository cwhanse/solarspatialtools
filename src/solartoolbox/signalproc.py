import numpy as np
import pandas as pd
from scipy import signal
from scipy.interpolate import interp1d
import pvlib

from solartoolbox import spatial


# Library for Analyzing Time Series Transfer Functions
def averaged_psd(input_tsig, navgs, overlap=0.5,
                 window='hanning', detrend='linear', scaling='density'):
    """
    Calculate an averaged power spectral density for a signal.

    Parameters
    ----------
    input_tsig : numeric
        Pandas type with the TF input time signal. Index must be time.

    navgs : int
        The number of averages to use based on zero overlap. Overlap will
        result in more averages.

    overlap : float (default 0.5)
        Percentage overlap between the averages

    window : string (default 'hanning')
        The window type to use.

    detrend : string
        Detrend type ('linear' or 'constant'). See scipy.signal.welch for more
        information.

    scaling : string (default 'density')
        The type of scaling to request from scipy. See scipy.signal.welch for
        more info

    Returns
    -------
    output : Series
        Pandas Series containing the power spectral density with an index of
        the frequency.
    """
    dt = (input_tsig.index[1] - input_tsig.index[0]).total_seconds()
    fs = 1/dt

    nperseg = int(len(input_tsig) // navgs)
    noverlap = int(nperseg * overlap)
    f, psdxx = signal.welch(input_tsig, fs=fs, window=window,
                            nperseg=nperseg, detrend=detrend,
                            noverlap=noverlap, scaling=scaling)
    # Reported units from scipy are V**2/Hz
    return pd.Series(psdxx, index=f)


def averaged_tf(input_tsig, output_tsig,
                navgs, overlap=0.5, window='hanning', detrend='linear'):
    """
    Calculate a transfer function between two signals, along with their
    coherence.

    Parameters
    ----------
    input_tsig : numeric
        Pandas type with the TF input time signal. Index must be time.

    output_tsig : numeric
        Pandas type with the TF output time signal. Index must be time.

    navgs : int
        The number of averages to use based on zero overlap. Overlap will
        result in more averages.

    overlap : float (default 0.5)
        Percentage overlap between the averages

    window : string (default 'hanning')
        The window type to use.

    detrend : string
        Detrend type ('linear' or 'constant'). See scipy.signal.psdxx for more
        information.

    Returns
    -------
    output : DataFrame
        Pandas object containing the transfer function and coherence with an
        index of the frequency.
        Columns are:
            'tf' - the complex transfer function
            'coh' - the coherence
    """

    dt = (input_tsig.index[1] - input_tsig.index[0]).total_seconds()
    fs = 1/dt

    nperseg = int(len(input_tsig) // navgs)
    noverlap = int(nperseg * overlap)

    # Calculate the transfer function
    psdxx = averaged_psd(input_tsig, window=window, navgs=navgs,
                         detrend=detrend, overlap=overlap, scaling='density')
    _, csdxy = signal.csd(input_tsig, output_tsig, fs=fs, window=window,
                          nperseg=nperseg, detrend=detrend,
                          noverlap=noverlap)
    tf = csdxy / psdxx

    # Calculate the coherence
    _, coh = signal.coherence(input_tsig, output_tsig, fs=fs, window=window,
                              nperseg=nperseg, noverlap=noverlap,
                              detrend=detrend)

    output = pd.DataFrame({'tf': tf, 'coh': coh}, index=psdxx.index)

    return output


def interp_tf(new_freq, input_tf):
    """
    Interpolate a transfer function in the frequency domain by magnitude and
    phase independently. This is necessary because the complex interpolation
    doesn't really do the job on its own.

    Parameters
    ----------
    new_freq : np.array or pd.Index
        The new frequency index to interpolate onto.

    input_tf : pd.Series or pd.DataFrame
        The transfer function to be interpolated

    Returns
    -------
    interp_tf : pd.Series or pd.DataFrame
        The transfer function interpolated to the new frequency axis. Type will
        match the type of the input.
    """
    sortinds = input_tf.index.argsort()
    if type(input_tf) is type(pd.Series()):
        use_tf = pd.DataFrame(input_tf)
    else:
        use_tf = input_tf

    # Generate a function handle and interpolate the magnitude
    interp_mag_func = interp1d(use_tf.index[sortinds],
                               np.abs(use_tf.iloc[sortinds, :]),
                               axis=0)
    interp_mag = interp_mag_func(new_freq)

    # Generate a function handle and interpolate the phase
    # Work on the unwrapped angle to make sure that we don't have weird
    # results in the middle of wraps.
    interp_phase_func = interp1d(use_tf.index[sortinds],
                                 np.unwrap(np.angle(use_tf.iloc[sortinds, :]),
                                           axis=0),
                                 axis=0)
    interp_phase = interp_phase_func(new_freq)

    # Recreate the complex TF
    interp_filt = interp_mag * np.exp(1j * interp_phase)

    # Appropriately recast the type
    if type(input_tf) is type(pd.Series()):
        interp_filt = pd.Series(interp_filt[:, 0], index=new_freq)
    else:
        interp_filt = pd.DataFrame(interp_filt, columns=input_tf.columns,
                                   index=new_freq)
    return interp_filt


def get_1d_plant(centers, ref_center=0,
                 width=None, shape="square",
                 dx=1, xmax=500000):
    """
    Generate a one dimensional plant array based on a list of center positions.
    Plant is essentially a comb filter with a site of a given shape placed at
    each specified center position

    Parameters
    ----------
    centers : numeric
        List of centers of the individual measurement locations. Commonly the
        output of spatial.project_vectors().

    ref_center : numeric
        Position of the reference, will be used as the zero of the x coordinate

    width : numeric
        The size of each individual plant component. If None, is equivalent to
        the dx for the plant.

    shape : string
        The shape to use for each individual plant component. Choices are:
        'square', 'triangle', 'gaussian'

    dx : numeric
        The x axis spacing to use for the numerical plant layout.

    xmax : numeric
        The maximum x size to use for the plant domain

    Returns
    -------
    plant : numeric
        A vector representing the plant's density of generation along the x
        axis

    x_vec : numeric
        The position axis for the plant.
    """

    if width is None:
        w = dx
    else:
        w = width

    centers = np.array(centers).flatten()
    centers -= ref_center

    # Initialize the empty plant
    x_vec = np.arange(-xmax//2, xmax//2, dx)
    plant = np.zeros(x_vec.shape, dtype=float)

    # Creating the individual plant windows ###############

    if shape.lower() == "square":
        # Square individual plants
        # Lc = L total plant
        # north = # of sites
        # Lc/north = separation
        for center in centers:
            inds = np.bitwise_and(x_vec >= (center - w / 2),
                                  x_vec < (center + w / 2))
            plant[inds] = 1
    elif shape.lower() == "triangle":
        for center in centers:
            inds = np.bitwise_and(x_vec >= (center - w / 2),
                                  x_vec < (center + w / 2))
            plant[inds] = x_vec[inds] - center+w/2
    elif shape.lower() == "gaussian":
        # Gaussian Window
        for center in centers:
            plant += np.exp(-(x_vec-center)**2/(2*(w/2.355)**2))  # FWHM is STD
    else:
        raise ValueError("No info for plant shape: {}".format(shape))

    return plant, x_vec


def plant1d_to_camfilter(plant, x_plant, cloud_speed):
    """
    Take a 1D plant and compute the Cloud Advection Model representation

    Parameters
    ----------
    cloud_speed : numeric
        The cloud motion vector speed

    plant : np.array
        An array-based representation of the plant generation density. Will be
        normalized to produce a transfer function DC magnitude of 1. See
        get_1d_plant().

    x_plant : np.array
        The plant's x-coordinate. Should have a value of zero at the location
        of the reference point. See get_1d_plant().

    Returns
    -------
    filter : pd.Series
        A pandas series with the complex valued transfer function, indexed by
        the corresponding frequency.
    """
    # TODO needs to be validated

    dx = x_plant[1]-x_plant[0]

    plant = plant / np.sum(plant)  # normalize the plant
    camfilt = np.fft.fft(plant)  # What does it look like in f domain
    spatialdt = dx / np.abs(cloud_speed)  # Effective dt for cloud motion
    camfreq = np.fft.fftfreq(plant.shape[-1], spatialdt)

    # Shift the phase
    t_delay = np.min(x_plant) / cloud_speed
    if cloud_speed > 0:
        camfilt = camfilt * np.exp(
            1j * camfreq * (2 * np.pi) * t_delay)
    else:
        camfilt = np.conj(
            camfilt * np.exp(1j * camfreq * (2 * np.pi) * -t_delay))
    return pd.Series(camfilt, index=camfreq)


def apply_filter(input_tsig, comp_filt):
    """
    Apply a filter to a signal, and return the filtered signal. Works to align
    the frequency axis of the computed filter with the

    Parameters
    ----------
    input_tsig : pandas.Series or DataFrame
        Pandas type that contains the time signal

    comp_filt : Series, DataFrame
        Pandas type containing the complex valued filter to apply with its
        frequency in the index. See for example: get_camfilter

    Returns
    -------
    filtered_sig : numeric
        The filtered time series.
    """
    # Get the fft of the input signal, including its frequency axis
    dt = (input_tsig.index[1] - input_tsig.index[0]).total_seconds()
    input_fft = np.fft.fft(input_tsig) * 2 / len(input_tsig)
    f_vec = np.fft.fftfreq(input_tsig.shape[-1], dt)

    if np.max(f_vec) > np.max(comp_filt.index):
        raise ValueError('Error: the TF to apply does not cover the entire '
                         'frequency axis needed for the signal. Please '
                         'provide a TF with a higher maximum frequency.')

    # Interpolate the computational
    interp_filt = interp_tf(f_vec, comp_filt)

    # Apply the filter and invert.
    filtered_fft = input_fft * interp_filt
    filtered_sig = np.fft.ifft(filtered_fft * len(input_tsig) / 2)
    filtered_sig = np.real(filtered_sig)
    filtered_sig = pd.Series(filtered_sig, index=input_tsig.index)

    return filtered_sig


def get_camfilter(positions, cloud_speed, cloud_dir, ref_id, dx=1, **kwargs):
    """
    Compute the filter for the CAM model

    Parameters
    ----------
    positions : pandas.DataFrame
        Pandas object containing locations of each reference site within the
        overall plant. Must be indexed by the site id. See data storage format.

        If positions contain 'lat' and 'lon' columns, they will be converted
        to UTM assuming latitude and longitude in degrees. Otherwise, it will
        be assumed that they are already in a UTM-like coordinate system.

    cloud_speed : numeric
        The cloud motion speed

    cloud_dir : tuple
        A tuple (dx,dy) representing the cloud motion direction. Will be
        converted to a unit vector, so length is not important.

    ref_id : int
        The positional id for the reference site within positions.

    dx : numeric
        The spatial spacing that should be used in representing the plant.
        Affects the frequency band that can be represented.

    **kwargs : various
        Parameters that will be passed to get_1D_plant(). Include
            'width' - numeric width of each centered object
            'shape' - shape of each centered object (e.g. 'square')
            'xmax' - numeric maximum value in the spatial domain for the plant.
                     Affects the frequency resolution of the filter.
    Returns
    -------
    camfilter : Series
        A pandas Series containing the complex valued filter, along with its
        frequency vector along the index.
    """
    try:
        pos_utm = spatial.latlon2utm(positions['lat'], positions['lon'])
    except KeyError:
        pos_utm = positions

    pos_vecs = spatial.compute_vectors(pos_utm['E'], pos_utm['N'],
                                       pos_utm.loc[ref_id][['E', 'N']])
    pos_dists = spatial.project_vectors(pos_vecs, cloud_dir)

    plant, x_plant = get_1d_plant(pos_dists, dx=dx, **kwargs)
    camfilter = plant1d_to_camfilter(plant, x_plant, cloud_speed)
    return camfilter


def get_marcosfilter(s, freq=None):
    """
    Compute the filter for the Marcos model

    Parameters
    ----------
    s : numeric
        plant size in Hectares

    freq : numeric (default None)
        A vector of frequencies to include. A reference array will be computed
        if no frequency is provided.

    Returns
    -------
    output : Series
        A pandas Series with the complex valued filter. Index is frequency.

    """
    if freq is None:
        freq = np.linspace(0, 0.5, 100)
    k = 1
    fc = 0.02 / np.sqrt(s)
    filt = k / (1j * freq / fc + 1)
    return pd.Series(filt, index=freq, dtype=np.complex64)


def cleanfreq(sig):
    """
    Cleanup the bidirectional frequencies of a filter object for better
    visualization without lines wrapping across the zero.

    Parameters
    ----------
    sig : pandas.Series
        An object with an index of frequency that will be adjusted

    Returns
    -------
    The signal object with modified frequency
    """
    idxlist = sig.index.to_list()
    idxlist[len(sig.index) // 2] = None
    sig.index = idxlist
