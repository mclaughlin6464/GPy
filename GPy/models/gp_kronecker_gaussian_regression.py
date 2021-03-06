# Copyright (c) 2014, James Hensman, Alan Saul
# Licensed under the BSD 3-clause license (see LICENSE.txt)

import numpy as np
from ..core import Model
from paramz import ObsAr
from .. import likelihoods
from functools import reduce

class GPKroneckerGaussianRegression(Model):
    """
    Kronecker GP regression

    Take kernels computed on separate spaces K1(X1), K2(X2), ... KN(XN) and a data
    matrix Y which is of size (N1, N2, ... NN).

    The effective covaraince is np.kron(KN, ..., K2, K1)
    The effective data is vec(Y) = Y.flatten(order='F')

    The noise must be iid Gaussian.

    See [stegle_et_al_2011]_.

    .. rubric:: References

    .. [stegle_et_al_2011] Stegle, O.; Lippert, C.; Mooij, J.M.; Lawrence, N.D.; Borgwardt, K.:Efficient inference in matrix-variate Gaussian models with \iid observation noise. In: Advances in Neural Information Processing Systems, 2011, Pages 630-638

    """
    def __init__(self, X1, X2,  Y, kern1, kern2, noise_var=1., name='KGPR', additional_Xs = [], additional_kerns = []):
        super(GPKroneckerGaussianRegression, self).__init__(name=name)

        Xs = [X1, X2]
        Xs.extend(additional_Xs)

        kerns = [kern1, kern2]
        kerns.extend(additional_kerns)
        assert len(Xs) == len(kerns), "Xs and Kernels must have the same length"

        # accept the construction arguments
        for i, (X, kern) in enumerate(zip(Xs, kerns)):

            assert len(X.shape) > 1, "Invalid X shape, need at least two dimensions"
            assert X.shape[0] == Y.shape[i], "Invalid shape in dimension %d of Y"%i
            assert kern.input_dim == X.shape[1], "Invalid shape dimension %d of kernel"%i

            setattr(self, "num_data%d"%i, X.shape[0])
            setattr(self, "input_dim%d"%i, X.shape[1])

            setattr(self, "X%d"%i, ObsAr(X))
            setattr(self, "kern%d"%i, kern)

            self.link_parameter(kern)

        self.Y = Y
        #self.rotated_Y_shape = np.swapaxes(Y, 0,1).shape

        self.likelihood = likelihoods.Gaussian()
        self.likelihood.variance = noise_var
        self.link_parameter(self.likelihood)

    def log_likelihood(self):
        return self._log_marginal_likelihood

    def parameters_changed(self):
        dims = len(self.Y.shape)
        Ss, Us = [], []
        for i in range(dims):
            X = getattr(self, "X%d"%i)
            kern = getattr(self, "kern%d"%i)

            K = kern.K(X)
            S, U = np.linalg.eigh(K)

            #if i==1:
            #    Ss.insert(0, S)
            #    Us.insert(0, U)
            #else:
            Ss.append(S)
            Us.append(U)

        # ^^^ swap the orders of the first and second elements
        # this is only necessary to make sure things are consistent with non-kronecker kernels
        # mathematically theyr're the same.

        W = reduce(np.kron, reversed(Ss))

        W+=self.likelihood.variance

        #rotated_Y = np.swapaxes(self.Y, 0,1)
        Y_list = [self.Y]
        Y_list.extend(Us)

        Y_ = reduce(lambda x,y: np.tensordot(x, y.T, axes=[[0],[1]]), Y_list)
        Wi = 1./W
        Ytilde = Y_.flatten(order='F')*Wi
        num_data_prod = np.prod([getattr(self, "num_data%d"%i) for i in range(len(self.Y.shape))])

        self._log_marginal_likelihood = -0.5*num_data_prod*np.log(2*np.pi)\
                                        -0.5*np.sum(np.log(W))\
                                        -0.5*np.dot(Y_.flatten(order='F'), Ytilde)

        # gradients for data fit part
        Yt_reshaped = np.reshape(Ytilde, self.Y.shape, order='F')
        Wi_reshaped = np.reshape(Wi, self.Y.shape, order='F')

        for i in range(dims):
            U = Us[i]
            tmp =np.tensordot(U.T, Yt_reshaped, axes = [[0], [i]])
            S = reduce(np.multiply.outer, [s for j,s in enumerate(Ss) if i!=j])

            tmps = tmp*S
            # NOTE not pleased about the construction of these axes. Should be able to use a simpler 
            # integer input to axes, but in practice it didn't seem to work.

            axes = [[k for k in range(dims-1, 0, -1)], [j for j in range(dims-1)]]
            dL_dK = .5 * (np.tensordot(tmps, tmp.T, axes = axes))

            axes = [[k for k in range(dims-1, -1, -1) if k!=i], [j for j in range(dims - 1)]]
            tmp = np.tensordot(Wi_reshaped, S.T, axes=axes)

            dL_dK+=-0.5*np.dot(U*tmp, U.T)

            getattr(self, "kern%d"%i).update_gradients_full(dL_dK, getattr(self, "X%d"%i))

        # gradients for noise variance
        dL_dsigma2 = -0.5*Wi.sum() + 0.5*np.sum(np.square(Ytilde))
        self.likelihood.variance.gradient = dL_dsigma2

        # store these quantities for prediction:
        self.Wi, self.Ytilde = Wi, Ytilde

        for i,u in enumerate(Us):
            setattr(self, "U%d"%i, u)

    def predict(self, X1new, X2new, mean_only= False, additional_Xnews = []):

        """
        Return the predictive mean and variance at a series of new points X1new, X2new
        Only returns the diagonal of the predictive variance, for now.

        :param Xnews: A list of len(dims), with points at which to make a prediction
        :type Xnew: iterable, len(dims) where each element is Nxself.input_dim_i
        :param mean_only: Flag to only predict the mean. The variance is generally much slower to compute than the mean.
        :type mean_only: Bool
        """
        Xnews = [X1new, X2new]
        Xnews.extend(additional_Xnews)

        embeds = []
        kxxs = []
        dims = len(self.Y.shape)
        assert len(Xnews) == dims, "Invalid number of Xnews"

        for i in range(dims):
            kern = getattr(self, "kern%d"%i)
            kxf = kern.K(Xnews[i], getattr(self, "X%d"%i))

            # same as above swap orders for consistancy
            #if i == 1:
            #    embeds.insert(0,kcf.dot(getattr(self, "U%d"%i)))
            #    kxxs.insert(0, kern.Kdiag(Xnews[i]))
            #else:
            embeds.append(kxf.dot(getattr(self, "U%d"%i)))
            kxxs.append(kern.Kdiag(Xnews[i]))

        Y_list = [self.Ytilde.reshape(self.Y.shape, order = 'F')]
        Y_list.extend(embeds)

        mu = reduce(lambda x,y: np.tensordot(x,y, axes=[[0],[1]]), Y_list).flatten(order='F')
        #print mu.shape
        if mean_only:
            return mu[:,None], None

        kron_embeds = reduce(np.kron, reversed(embeds))
        var = reduce(np.kron, reversed(kxxs))- np.sum(kron_embeds**2*self.Wi, 1) + self.likelihood.variance
        #print var.shape
        return mu[:, None], var[:, None]
