from math import log, sqrt, log1p, exp
import numpy as np
from scipy import linalg

from mne.inverse_sparse.mxne_optim import groups_norm2
from bayes_meeg.pyrtnorm import rtnorm
from numba import jit, float64


@jit(float64(float64, float64), nopython=True, nogil=True)
def _cond_gamma_hyperprior_sampler(coupling, beta):
    # compute maximal value
    if coupling == 0:
        gamma = - beta * log(np.random.rand())
        return gamma

    gammaMax = sqrt(beta * coupling)
    # corresponding probability
    logPmax = - coupling / gammaMax - gammaMax / beta
    # compute point in which exponetial tail umbrella is used
    xCeil = (beta * coupling) / gammaMax + gammaMax
    # compute the probability mass of the umbrella over and below xCeil and
    # total mass
    logPMassOverCeil = log(beta) - xCeil / beta
    logPMassBelowXCeil = logPmax + log(xCeil)
    logPMassTotal = logPMassOverCeil + \
        log1p(exp(logPMassBelowXCeil - logPMassOverCeil))

    while True:
        w, u, v = np.random.rand(), np.random.rand(), np.random.rand()
        # flip a coin in which area of the umbrella we are, use logarithms
        if (log(w) + logPMassTotal) < logPMassOverCeil:
            # we are in the tail, generate an exponentially distributed
            # random variable truncated to [xCeil,infty]
            logV = log(v) - xCeil / beta  # rescale probability v to [0,pCeil]
            gamma = - beta * logV
            # acceptance step
            if (log(u) * beta) < (coupling / logV):
                break
        else:
            # draw from rectangle
            gamma = v * xCeil
            # acceptance step
            if (log(u) + logPmax) < (-coupling / gamma - gamma / beta):
                break

    return gamma


def cond_gamma_hyperprior_sampler(coupling, beta):
    r"""Sample from distribution of the form

    p(gamma) \prop exp(- coupling / gamma) exp(- gamma / beta)
    """
    if isinstance(coupling, float):
        gamma = _cond_gamma_hyperprior_sampler(coupling, beta)
    else:
        gamma = np.empty(len(coupling))
        for i in range(len(coupling)):
            gamma[i] = _cond_gamma_hyperprior_sampler(coupling[i], beta)

    return gamma


def sc_slice_sampler(a, b, c, d, x0, n_samples):
    r"""Sample from

    p(x) \prop exp(-a x^2 + b x - c \sqrt{x^2 + d})
    """
    if not(a == 0 and b == 0):
        sigma = 1. / sqrt(2. * a)
        mu = b / (2 * a)
    else:
        raise ValueError('this should not happen')

    x = x0
    for k in range(n_samples):
        # sample aux variable y
        log_gy = -c * (sqrt(x**2 + d))

        t = np.random.rand()
        log_y = log_gy + log(t)

        # solve for xi
        xi = sqrt((-log_y / c)**2 - d)

        if xi > 0:  # otherwise, there is no interval to sample from
            x = rtnorm(a=-xi, b=xi, mu=mu, sigma=sigma, size=1)
        else:
            x = 0

    return x


# @profile
def L21_gamma_hypermodel_sampler(M, G, X0, gammas, n_orient, beta, n_burnin,
                                 n_samples, sc_n_samples=10, ss_n_samples=200):
    rng = np.random.RandomState(42)
    n_dipoles = G.shape[1]
    n_locations = n_dipoles // n_orient
    _, n_times = M.shape

    XChain = np.zeros((n_dipoles, n_times, n_samples))
    gammaChain = np.zeros((n_locations, n_samples))

    # precompute some terms
    GColSqNorm = np.sum(G ** 2, axis=0)
    GTM = np.dot(G.T, M)
    GTG = np.dot(G.T, G)

    if not X0.all():
        X = np.zeros((n_locations * n_orient, n_times))
    else:
        X = X0

    for k in range(-n_burnin, n_samples):
        print("Running iter %d" % k)
        # update X by single component Gibbs sampler
        # initialize with 0 instead of current state (this had a proper reason,
        # but we should re-examine)
        # X = np.zeros((n_locations * n_orient, n_times))

        for kSCGibbs in range(sc_n_samples):
            # print(" -- Running SC iter %d" % kSCGibbs)
            # randLocOrder = rng.randint(n_locations, size=n_locations)
            randLocOrder = rng.permutation(n_locations)
            for jLoc in randLocOrder:
                # a only depends on the location
                a = GColSqNorm[jLoc] / 2.
                c = 1. / gammas[jLoc]

                # extract X for this location
                XLoc = X[jLoc * n_orient: (jLoc + 1) * n_orient, :]
                XLocSqNorm = linalg.norm(XLoc, 'fro') ** 2

                # update all time points and all dir without random shuffle
                for jTime in range(n_times):
                    for jDir in range(n_orient):
                        # get corresponding dipole, time and block index
                        jComp = jDir + jLoc * n_orient
                        XjComp = X[jComp, jTime]
                        # compute b and d
                        b = GTM[jComp, jTime] - np.dot(X[:, jTime].T,
                                                       GTG[:, jComp]) + \
                            2 * a * XjComp
                        d = XLocSqNorm - XjComp**2
                        # call slice sampler
                        XjComp = sc_slice_sampler(
                            a, b, c, d, XjComp, ss_n_samples)
                        # update auxillary variables
                        XLocSqNorm = d + XjComp**2
                        X[jComp, jTime] = XjComp

        # check for instabilities cause by insufficient sampling steps,
        # usually leading to an explosion of the residual
        # if (linalg.norm(G.dot(X) - M, 'fro') / linalg.norm(M, 'fro')) > 10:
        #     raise ValueError('relative residual exceeded threshold, '
        #                      'the sampler is likely to diverge due to '
        #                      'insufficient precision in the block-sampling')

        # update gamma by umbrella sampler
        # Compute the amplitudes of the sources for one hyperparameter
        XBlkNorm = np.sqrt(groups_norm2(X.copy(), n_orient))
        gammas = cond_gamma_hyperprior_sampler(XBlkNorm, beta)

        # store results
        if k >= 0:
            XChain[:, :, k] = X
            gammaChain[:, k] = gammas

    return XChain, gammaChain


if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from scipy.integrate import quad

    size = 10000
    beta = 1.
    coupling = 1.
    couplings = coupling * np.ones(size)

    gammas = cond_gamma_hyperprior_sampler(couplings[:1], beta)

    import time
    t0 = time.time()
    gammas = cond_gamma_hyperprior_sampler(couplings, beta)
    print(time.time() - t0)

    plt.close('all')

    xmin, xmax = np.min(gammas), np.max(gammas)
    xx = np.linspace(xmin, xmax, 1000)

    def dist(xx):
        return np.exp(- coupling / xx) * np.exp(- xx / beta)

    Z, _ = quad(dist, 1e-5, 20)

    plt.figure()
    plt.hist(gammas, normed=True, bins=20)
    plt.plot(xx, dist(xx) / Z, 'r', linewidth=2)
    plt.show()

    (a, b, c, d), x0, n_samples = (1,) * 4, 0., 1000
    chain = sc_slice_sampler(a, b, c, d, x0, n_samples)

    def dist(xx):
        return np.exp(-a * xx ** 2 + b * xx - c * np.sqrt(xx ** 2 + d))
    xx = np.linspace(-2, 3, 300)

    Z, _ = quad(dist, -5, 5)

    plt.figure()
    plt.hist(chain, normed=True, bins=20)
    plt.plot(xx, dist(xx) / Z, 'r', linewidth=2)
    plt.show()
