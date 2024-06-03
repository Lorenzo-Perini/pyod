"""Single-Objective Generative Adversarial Active Learning.
Part of the codes are adapted from
https://github.com/leibinghe/GAAL-based-outlier-detection
"""
# Author: Sihan Chen <schen976@usc.edu>
# License: BSD 2 clause

import math
from collections import defaultdict

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted
from torch.utils.data import DataLoader, TensorDataset

from .base import BaseDetector


class Generator(nn.Module):
    def __init__(self, latent_size):
        super(Generator, self).__init__()
        self.layer1 = nn.Linear(latent_size, latent_size)
        self.layer2 = nn.Linear(latent_size, latent_size)
        nn.init.eye_(self.layer1.weight)
        nn.init.eye_(self.layer2.weight)

    def forward(self, x):
        x = F.relu(self.layer1(x))
        x = F.relu(self.layer2(x))
        return x


class Discriminator(nn.Module):
    def __init__(self, latent_size, data_size):
        super(Discriminator, self).__init__()
        self.layer1 = nn.Linear(latent_size, math.ceil(math.sqrt(data_size)))
        self.layer2 = nn.Linear(math.ceil(math.sqrt(data_size)), 1)
        nn.init.kaiming_normal_(self.layer1.weight, mode='fan_in',
                                nonlinearity='relu')
        nn.init.kaiming_normal_(self.layer2.weight, mode='fan_in',
                                nonlinearity='sigmoid')

    def forward(self, x):
        x = F.relu(self.layer1(x))
        x = torch.sigmoid(self.layer2(x))
        return x


class SO_GAAL(BaseDetector):
    """Single-Objective Generative Adversarial Active Learning.

    SO-GAAL directly generates informative potential outliers to assist the
    classifier in describing a boundary that can separate outliers from normal
    data effectively. Moreover, to prevent the generator from falling into the
    mode collapsing problem, the network structure of SO-GAAL is expanded from
    a single generator (SO-GAAL) to multiple generators with different
    objectives (MO-GAAL) to generate a reasonable reference distribution for
    the whole dataset.
    Read more in the :cite:`liu2019generative`.

    Parameters
    ----------
    contamination : float in (0., 0.5), optional (default=0.1)
        The amount of contamination of the data set, i.e.
        the proportion of outliers in the data set. Used when fitting to
        define the threshold on the decision function.

    stop_epochs : int, optional (default=20)
        The number of epochs of training. The number of total epochs equals to
         three times of stop_epochs.

    lr_d : float, optional (default=0.01)
        The learn rate of the discriminator.

    lr_g : float, optional (default=0.0001)
        The learn rate of the generator.

    momentum : float, optional (default=0.9)
        The momentum parameter for SGD.

    Attributes
    ----------
    decision_scores_ : numpy array of shape (n_samples,)
        The outlier scores of the training data.
        The higher, the more abnormal. Outliers tend to have higher
        scores. This value is available once the detector is fitted.

    threshold_ : float
        The threshold is based on ``contamination``. It is the
        ``n_samples * contamination`` most abnormal samples in
        ``decision_scores_``. The threshold is calculated for generating
        binary outlier labels.

    labels_ : int, either 0 or 1
        The binary labels of the training data. 0 stands for inliers
        and 1 for outliers/anomalies. It is generated by applying
        ``threshold_`` on ``decision_scores_``.
    """

    def __init__(self, stop_epochs=20, lr_d=0.01, lr_g=0.0001, momentum=0.9,
                 contamination=0.1):
        super(SO_GAAL, self).__init__(contamination=contamination)
        self.stop_epochs = stop_epochs
        self.lr_d = lr_d
        self.lr_g = lr_g
        self.momentum = momentum

    def fit(self, X, y=None):
        """Fit detector. y is ignored in unsupervised methods.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The input samples.

        y : Ignored
            Not used, present for API consistency by convention.

        Returns
        -------
        self : object
            Fitted estimator.
        """
        X = check_array(X)
        self._set_n_classes(y)
        latent_size = X.shape[1]
        data_size = X.shape[0]
        stop = 0
        epochs = self.stop_epochs * 3
        self.train_history = defaultdict(list)

        self.discriminator = Discriminator(latent_size, data_size)
        self.generator = Generator(latent_size)

        optimizer_d = optim.SGD(self.discriminator.parameters(), lr=self.lr_d,
                                momentum=self.momentum)
        optimizer_g = optim.SGD(self.generator.parameters(), lr=self.lr_g,
                                momentum=self.momentum)
        criterion = nn.BCELoss()

        dataloader = DataLoader(
            TensorDataset(torch.tensor(X, dtype=torch.float32)),
            batch_size=min(500, data_size),
            shuffle=True)

        for epoch in range(epochs):
            print('Epoch {} of {}'.format(epoch + 1, epochs))

            for data_batch in dataloader:
                data_batch = data_batch[0]
                batch_size = data_batch.size(0)

                # Train Discriminator
                noise = torch.rand(batch_size, latent_size)
                generated_data = self.generator(noise)

                real_labels = torch.ones(batch_size, 1)
                fake_labels = torch.zeros(batch_size, 1)

                outputs_real = self.discriminator(data_batch)
                outputs_fake = self.discriminator(generated_data)

                d_loss_real = criterion(outputs_real, real_labels)
                d_loss_fake = criterion(outputs_fake, fake_labels)

                d_loss = d_loss_real + d_loss_fake

                optimizer_d.zero_grad()
                d_loss.backward()
                optimizer_d.step()

                self.train_history['discriminator_loss'].append(d_loss.item())

                if stop == 0:
                    # Train Generator
                    trick_labels = torch.ones(batch_size, 1)
                    g_loss = criterion(
                        self.discriminator(self.generator(noise)),
                        trick_labels)

                    optimizer_g.zero_grad()
                    g_loss.backward()
                    optimizer_g.step()

                    self.train_history['generator_loss'].append(g_loss.item())
                else:
                    g_loss = criterion(
                        self.discriminator(self.generator(noise)),
                        trick_labels)
                    self.train_history['generator_loss'].append(g_loss.item())

            if epoch + 1 > self.stop_epochs:
                stop = 1

        self.decision_scores_ = self.discriminator(
            torch.tensor(X, dtype=torch.float32)).detach().numpy().ravel()
        self._process_decision_scores()
        return self

    def decision_function(self, X):
        """Predict raw anomaly score of X using the fitted detector.

        The anomaly score of an input sample is computed based on different
        detector algorithms. For consistency, outliers are assigned with
        larger anomaly scores.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The training input samples. Sparse matrices are accepted only
            if they are supported by the base estimator.

        Returns
        -------
        anomaly_scores : numpy array of shape (n_samples,)
            The anomaly score of the input samples.
        """
        check_is_fitted(self, ['discriminator'])
        X = check_array(X)
        pred_scores = self.discriminator(
            torch.tensor(X, dtype=torch.float32)).detach().numpy().ravel()
        return pred_scores