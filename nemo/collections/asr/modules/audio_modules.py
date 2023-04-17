# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Dict, Optional, Tuple

import numpy as np
import torch

from nemo.collections.asr.parts.preprocessing.features import make_seq_mask_like
from nemo.collections.asr.parts.submodules.multichannel_modules import ParametricMultichannelWienerFilter
from nemo.collections.asr.parts.utils.audio_utils import db2mag, wrap_to_pi
from nemo.core.classes import NeuralModule, typecheck
from nemo.core.neural_types import FloatType, LengthsType, NeuralType, SpectrogramType
from nemo.utils import logging
from nemo.utils.decorators import experimental

try:
    import torchaudio

    HAVE_TORCHAUDIO = True
except ModuleNotFoundError:
    HAVE_TORCHAUDIO = False


__all__ = [
    'MaskEstimatorRNN',
    'MaskReferenceChannel',
    'MaskBasedBeamformer',
    'MaskBasedDereverbWPE',
]


@experimental
class SpectrogramToMultichannelFeatures(NeuralModule):
    """Convert a complex-valued multi-channel spectrogram to
    multichannel features.

    Args:
        num_subbands: Expected number of subbands in the input signal
        num_input_channels: Optional, provides the number of channels
                            of the input signal. Used to infer the number
                            of output channels.
        magnitude_reduction: Reduction across channels. Default `None`, will calculate
                             magnitude of each channel.
        use_ipd: Use inter-channel phase difference (IPD).
        mag_normalization: Normalization for magnitude features
        ipd_normalization: Normalization for IPD features
    """

    def __init__(
        self,
        num_subbands: int,
        num_input_channels: Optional[int] = None,
        mag_reduction: Optional[str] = 'rms',
        use_ipd: bool = False,
        mag_normalization: Optional[str] = None,
        ipd_normalization: Optional[str] = None,
    ):
        super().__init__()
        self.mag_reduction = mag_reduction
        self.use_ipd = use_ipd

        # TODO: normalization
        if mag_normalization is not None:
            raise NotImplementedError(f'Unknown magnitude normalization {mag_normalization}')
        self.mag_normalization = mag_normalization

        if ipd_normalization is not None:
            raise NotImplementedError(f'Unknown ipd normalization {ipd_normalization}')
        self.ipd_normalization = ipd_normalization

        if self.use_ipd:
            self._num_features = 2 * num_subbands
            self._num_channels = num_input_channels
        else:
            self._num_features = num_subbands
            self._num_channels = num_input_channels if self.mag_reduction is None else 1

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "input_length": NeuralType(('B',), LengthsType()),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "output_length": NeuralType(('B',), LengthsType()),
        }

    @property
    def num_features(self) -> int:
        """Configured number of features
        """
        return self._num_features

    @property
    def num_channels(self) -> int:
        """Configured number of channels
        """
        if self._num_channels is not None:
            return self._num_channels
        else:
            raise ValueError(
                'Num channels is not configured. To configure this, `num_input_channels` '
                'must be provided when constructing the object.'
            )

    @typecheck()
    def forward(self, input: torch.Tensor, input_length: torch.Tensor) -> torch.Tensor:
        """Convert input batch of C-channel spectrograms into
        a batch of time-frequency features with dimension num_feat.
        The output number of channels may be the same as input, or
        reduced to 1, e.g., if averaging over magnitude and not appending individual IPDs.

        Args:
            input: Spectrogram for C channels with F subbands and N time frames, (B, C, F, N)
            input_length: Length of valid entries along the time dimension, shape (B,)

        Returns:
            num_feat_channels channels with num_feat features, shape (B, num_feat_channels, num_feat, N)
        """
        # Magnitude spectrum
        if self.mag_reduction is None:
            mag = torch.abs(input)
        elif self.mag_reduction == 'abs_mean':
            mag = torch.abs(torch.mean(input, axis=1, keepdim=True))
        elif self.mag_reduction == 'mean_abs':
            mag = torch.mean(torch.abs(input), axis=1, keepdim=True)
        elif self.mag_reduction == 'rms':
            mag = torch.sqrt(torch.mean(torch.abs(input) ** 2, axis=1, keepdim=True))
        else:
            raise ValueError(f'Unexpected magnitude reduction {self.mag_reduction}')

        if self.mag_normalization is not None:
            mag = self.mag_normalization(mag)

        features = mag

        if self.use_ipd:
            # Calculate IPD relative to average spec
            spec_mean = torch.mean(input, axis=1, keepdim=True)
            ipd = torch.angle(input) - torch.angle(spec_mean)
            # Modulo to [-pi, pi]
            ipd = wrap_to_pi(ipd)

            if self.ipd_normalization is not None:
                ipd = self.ipd_normalization(ipd)

            # Concatenate to existing features
            features = torch.cat([features.expand(ipd.shape), ipd], axis=2)

        if self._num_channels is not None and features.size(1) != self._num_channels:
            raise RuntimeError(
                f'Number of channels in features {features.size(1)} is different than the configured number of channels {self._num_channels}'
            )

        return features, input_length


class MaskEstimatorRNN(NeuralModule):
    """Estimate `num_outputs` masks from the input spectrogram
    using stacked RNNs and projections.

    The module is structured as follows:
        input --> spatial features --> input projection -->
            --> stacked RNNs --> output projection for each output --> sigmoid

    Reference:
        Multi-microphone neural speech separation for far-field multi-talker
        speech recognition (https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=8462081)

    Args:
        num_outputs: Number of output masks to estimate
        num_subbands: Number of subbands of the input spectrogram
        num_features: Number of features after the input projections
        num_layers: Number of RNN layers
        num_hidden_features: Number of hidden features in RNN layers
        num_input_channels: Number of input channels
        dropout: If non-zero, introduces dropout on the outputs of each RNN layer except the last layer, with dropout
                 probability equal to `dropout`. Default: 0
        bidirectional: If `True`, use bidirectional RNN.
        rnn_type: Type of RNN, either `lstm` or `gru`. Default: `lstm`
        mag_reduction: Channel-wise reduction for magnitude features
        use_ipd: Use inter-channel phase difference (IPD) features
    """

    def __init__(
        self,
        num_outputs: int,
        num_subbands: int,
        num_features: int = 1024,
        num_layers: int = 3,
        num_hidden_features: Optional[int] = None,
        num_input_channels: Optional[int] = None,
        dropout: float = 0,
        bidirectional=True,
        rnn_type: str = 'lstm',
        mag_reduction: str = 'rms',
        use_ipd: bool = None,
    ):
        super().__init__()
        if num_hidden_features is None:
            num_hidden_features = num_features

        self.features = SpectrogramToMultichannelFeatures(
            num_subbands=num_subbands,
            num_input_channels=num_input_channels,
            mag_reduction=mag_reduction,
            use_ipd=use_ipd,
        )

        self.input_projection = torch.nn.Linear(
            in_features=self.features.num_features * self.features.num_channels, out_features=num_features
        )

        if rnn_type == 'lstm':
            self.rnn = torch.nn.LSTM(
                input_size=num_features,
                hidden_size=num_hidden_features,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
                bidirectional=bidirectional,
            )
        elif rnn_type == 'gru':
            self.rnn = torch.nn.GRU(
                input_size=num_features,
                hidden_size=num_hidden_features,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout,
                bidirectional=bidirectional,
            )
        else:
            raise ValueError(f'Unknown rnn_type: {rnn_type}')

        # Each output shares the RNN and has a separate projection
        self.output_projections = torch.nn.ModuleList(
            [
                torch.nn.Linear(
                    in_features=2 * num_features if bidirectional else num_features, out_features=num_subbands
                )
                for _ in range(num_outputs)
            ]
        )
        self.output_nonlinearity = torch.nn.Sigmoid()

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "input_length": NeuralType(('B',), LengthsType()),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), FloatType()),
            "output_length": NeuralType(('B',), LengthsType()),
        }

    @typecheck()
    def forward(self, input: torch.Tensor, input_length: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """Estimate `num_outputs` masks from the input spectrogram.

        Args:
            input: C-channel input, shape (B, C, F, N)
            input_length: Length of valid entries along the time dimension, shape (B,)

        Returns:
            Returns `num_outputs` masks in a tensor, shape (B, num_outputs, F, N),
            and output length with shape (B,)
        """
        input, _ = self.features(input=input, input_length=input_length)
        B, num_feature_channels, num_features, N = input.shape

        # (B, num_feat_channels, num_feat, N) -> (B, N, num_feat_channels, num_feat)
        input = input.permute(0, 3, 1, 2)

        # (B, N, num_feat_channels, num_feat) -> (B, N, num_feat_channels * num_features)
        input = input.view(B, N, -1)

        # Apply projection on num_feat
        input = self.input_projection(input)

        # Apply RNN on the input sequence
        input_packed = torch.nn.utils.rnn.pack_padded_sequence(
            input, input_length.cpu(), batch_first=True, enforce_sorted=False
        ).to(input.device)
        self.rnn.flatten_parameters()
        input_packed, _ = self.rnn(input_packed)
        input, input_length = torch.nn.utils.rnn.pad_packed_sequence(input_packed, batch_first=True)
        input_length = input_length.to(input.device)

        # Create `num_outputs` masks
        output = []
        for output_projection in self.output_projections:
            # Output projection
            mask = output_projection(input)
            mask = self.output_nonlinearity(mask)

            # Back to the original format
            # (B, N, F) -> (B, F, N)
            mask = mask.transpose(2, 1)

            # Append to the output
            output.append(mask)

        # Stack along channel dimension to get (B, M, F, N)
        output = torch.stack(output, axis=1)

        # Mask frames beyond input length
        length_mask: torch.Tensor = make_seq_mask_like(
            lengths=input_length, like=output, time_dim=-1, valid_ones=False
        )
        output = output.masked_fill(length_mask, 0.0)

        return output, input_length


class MaskEstimatorGSS(torch.nn.Module):
    """Estimate masks using guided source separation with a complex
    angular Central Gaussian Mixture Model (cACGMM).

    Notation is approximately following [1], where `gamma` denotes
    the time-frequency mask, `alpha` denotes the mixture weights,
    and `BM` denotes the shape matrix. Additionally, provided
    source activity is denoted as `activity`.

    Args:
        num_iterations: Number of iterations for the EM algorithm
        eps: Small value for regularization
        dtype: Data type for internal computations


    References:
        [1] Ito et al., Complex Angular Central Gaussian Mixture Model for Directional Statistics in Mask-Based Microphone Array Signal Processing, 2016
        [2] Boeddeker et al., Front-End Processing for the CHiME-5 Dinner Party Scenario, 2018
    """

    def __init__(self, num_iterations: int = 3, eps=1e-8, dtype: torch.dtype = torch.cdouble):
        super().__init__()

        self.num_iterations = num_iterations
        self.eps = eps
        # Internal calculations
        assert dtype in [torch.cfloat, torch.cdouble], f'Unsupported dtype {dtype}, expecting cfloat or cdouble'
        self.dtype = dtype

        logging.debug('Initialized %s', self.__class__.__name__)
        logging.debug('\tnum_iterations: %s', self.num_iterations)
        logging.debug('\teps:            %g', self.eps)
        logging.debug('\tdtype:          %s', self.dtype)

    def normalize(self, x: torch.Tensor, dim=-3) -> torch.Tensor:
        """Normalize input to have a unit L2-norm across `dim`.
        By default, normalizes across the input channels.

        Args:
            x: C-channel input signal, shape (B, C, F, T)
            dim: Dimension for normalization, defaults to -3 to normalize over channels

        Returns:
            Normalized signal, shape (B, C, F, T)
        """
        norm_x = torch.linalg.vector_norm(x, ord=2, dim=dim, keepdim=True)
        x = x / (norm_x + self.eps)
        return x

    def update_masks(self, alpha: torch.Tensor, activity, log_pdf):
        """Update masks for the cACGMM.

        Args:
            alpha: component weights, shape (B, num_outputs, F)
            activity: temporal activity for the components, shape (B, num_outputs, T)
            log_pdf: logarithm of the PDF, shape (B, num_outputs, F, T)

        Returns:
            Masks for the components of the model, shape (B, num_outputs, F, T)
        """
        num_inputs = log_pdf.size(-1)

        # (B, num_outputs, F)
        # normalize across outputs in the log domain
        gamma = log_pdf - torch.max(log_pdf, axis=-3, keepdim=True)[0]

        gamma = torch.exp(gamma)

        # calculate the mask using weight, pdf and source activity
        gamma = alpha[..., None] * gamma * activity[..., None, :]

        # normalize across components/output channels
        gamma = gamma / (torch.sum(gamma, dim=-3, keepdim=True) + self.eps)

        return gamma

    def update_weights(self, gamma):
        """Update weights for the individual components
        in the mixture model.

        Args:
            gamma: masks, shape (B, num_outputs, F, T)

        Returns:
            Component weights, shape (B, num_outputs, F)
        """
        alpha = torch.mean(gamma, dim=-1)
        return alpha

    def update_pdf(
        self, z: torch.Tensor, gamma: torch.Tensor, zH_invBM_z: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Update PDF of the cACGMM.

        Args:
            z: directional statistics, shape (B, num_inputs, F, T)
            gamma: masks, shape (B, num_outputs, F, T)
            zH_invBM_z: energy weighted by shape matrices, shape (B, num_outputs, F, T)

        Returns:
            Logarithm of the PDF, shape (B, num_outputs, F, T), the energy term, shape (B, num_outputs, F, T)
        """
        num_inputs = z.size(-3)

        # shape (B, num_outputs, F, T)
        scale = gamma / (zH_invBM_z + self.eps)

        # scale outer product and sum over time
        # shape (B, num_outputs, F, num_inputs, num_inputs)
        BM = num_inputs * torch.einsum('bmft,bift,bjft->bmfij', scale.to(z.dtype), z, z.conj())

        # normalize across time
        denom = torch.sum(gamma, dim=-1)
        BM = BM / (denom[..., None, None] + self.eps)

        # make sure the matrix is Hermitian
        BM = (BM + BM.conj().transpose(-1, -2)) / 2

        # use eigenvalue decomposition to calculate the log determinant
        # --
        # and the inverse-weighted energy term
        L, Q = torch.linalg.eigh(BM)

        # BM is positive definite, so all eigenvalues should be positive
        # However, small negative values may occur due to a limited precision
        L = torch.clamp(L.real, min=self.eps)

        # PDF is invariant to scaling of the shape matrix [1], so
        # eignevalues can be normalized (across num_inputs)
        L = L / (torch.max(L, axis=-1, keepdim=True)[0] + self.eps)

        # small regularization to avoid numerical issues
        L = L + self.eps

        # calculate the log determinant using the eigenvalues
        log_detBM = torch.sum(torch.log(L), dim=-1)

        # calculate the energy term using the inverse eigenvalues
        # --
        # zH_invBM_z = torch.einsum('bift,bmfij,bmfj,bmfkj,bkft->bmft', z.conj(), Q, (1 / L).to(Q.dtype), Q.conj(), z)
        # # small regularization
        # zH_invBM_z = zH_invBM_z.abs() + self.eps

        # calc sqrt(L) * Q^H * z
        zH_invBM_z = torch.einsum('bmfj,bmfkj,bkft->bmftj', (1 / L.sqrt()).to(Q.dtype), Q.conj(), z)
        # calc squared norm
        zH_invBM_z = zH_invBM_z.abs().pow(2).sum(-1)
        # small regularization
        zH_invBM_z = zH_invBM_z + self.eps

        # final log PDF
        # --
        log_pdf = -num_inputs * torch.log(zH_invBM_z) - log_detBM[..., None]

        return log_pdf, zH_invBM_z

    def forward(self, input: torch.Tensor, activity: torch.Tensor) -> torch.Tensor:
        """Apply GSS to estimate the masks.

        Args:
            input: batched C-channel input signal, shape (B, num_inputs, F, T)
            activity: batched frame-wise activity for each output/component, shape (B, num_outputs, T)

        Returns:
            Masks for the components of the model, shape (B, num_outputs, F, T)
        """
        B, num_inputs, F, T = input.shape
        num_outputs = activity.size(1)
        assert (
            activity.size(0) == B and activity.size(-1) == T
        ), f'Expecting activity of shape ({B}, num_outputs, {T}), got {activity.shape}'
        assert num_outputs > 1, f'Expecting multiple outputs, got {num_outputs}'

        with torch.cuda.amp.autocast(enabled=False):
            input = input.to(dtype=self.dtype)

            assert input.is_complex(), f'Expecting complex input, got {input.dtype}'

            # convert input to directional statistics
            # by normalizing across channels
            z = self.normalize(input, dim=-3)

            # initialize masks
            gamma = torch.clamp(activity, min=self.eps)
            # normalize across channels
            gamma = gamma / torch.sum(gamma, dim=-2, keepdim=True)
            # expand to input shape
            gamma = gamma.unsqueeze(2).expand(-1, -1, F, -1)

            # initialize energy term
            zH_invBM_z = torch.ones(B, num_outputs, F, T, dtype=input.dtype, device=input.device)

            # EM iterations
            for it in range(self.num_iterations):
                alpha = self.update_weights(gamma)
                log_pdf, zH_invBM_z = self.update_pdf(z, gamma, zH_invBM_z)
                gamma = self.update_masks(alpha, activity, log_pdf)

        if torch.any(torch.isnan(gamma)):
            raise RuntimeError(f'gamma contains NaNs: {gamma}')

        return gamma


class MaskReferenceChannel(NeuralModule):
    """A simple mask processor which applies mask
    on ref_channel of the input signal.

    Args:
        ref_channel: Index of the reference channel.
        mask_min_db: Threshold mask to a minimal value before applying it, defaults to -200dB
        mask_max_db: Threshold mask to a maximal value before applying it, defaults to 0dB
    """

    def __init__(self, ref_channel: int = 0, mask_min_db: float = -200, mask_max_db: float = 0):
        super().__init__()
        self.ref_channel = ref_channel
        # Mask thresholding
        self.mask_min = db2mag(mask_min_db)
        self.mask_max = db2mag(mask_max_db)

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "input_length": NeuralType(('B',), LengthsType()),
            "mask": NeuralType(('B', 'C', 'D', 'T'), FloatType()),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "output_length": NeuralType(('B',), LengthsType()),
        }

    @typecheck()
    def forward(
        self, input: torch.Tensor, input_length: torch.Tensor, mask: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Apply mask on `ref_channel` of the input signal.
        This can be used to generate multi-channel output.
        If `mask` has `M` channels, the output will have `M` channels as well.

        Args:
            input: Input signal complex-valued spectrogram, shape (B, C, F, N)
            input_length: Length of valid entries along the time dimension, shape (B,)
            mask: Mask for M outputs, shape (B, M, F, N)

        Returns:
            M-channel output complex-valed spectrogram with shape (B, M, F, N)
        """
        # Apply thresholds
        mask = torch.clamp(mask, min=self.mask_min, max=self.mask_max)

        # Apply each output mask on the ref channel
        output = mask * input[:, self.ref_channel : self.ref_channel + 1, ...]
        return output, input_length


class MaskBasedBeamformer(NeuralModule):
    """Multi-channel processor using masks to estimate signal statistics.

    Args:
        filter_type: string denoting the type of the filter. Defaults to `mvdr`
        filter_beta: Parameter of the parameteric multichannel Wiener filter
        filter_rank: Parameter of the parametric multichannel Wiener filter
        filter_postfilter: Optional, postprocessing of the filter
        ref_channel: Optional, reference channel. If None, it will be estimated automatically
        ref_hard: If true, hard (one-hot) reference. If false, a soft reference
        ref_hard_use_grad: If true, use straight-through gradient when using the hard reference
        ref_subband_weighting: If true, use subband weighting when estimating reference channel
        num_subbands: Optional, used to determine the parameter size for reference estimation
        mask_min_db: Threshold mask to a minimal value before applying it, defaults to -200dB
        mask_max_db: Threshold mask to a maximal value before applying it, defaults to 0dB
        diag_reg: Optional, diagonal regularization for the multichannel filter
        eps: Small regularization constant to avoid division by zero
    """

    def __init__(
        self,
        filter_type: str = 'mvdr_souden',
        filter_beta: float = 0.0,
        filter_rank: str = 'one',
        filter_postfilter: Optional[str] = None,
        filter_output_channel_reduction: Optional[str] = None,
        ref_channel: Optional[int] = 0,
        ref_hard: bool = True,
        ref_hard_use_grad: bool = False,
        ref_subband_weighting: bool = False,
        num_subbands: Optional[int] = None,
        mask_min_db: float = -200,
        mask_max_db: float = 0,
        postmask_min_db: float = 0,
        postmask_max_db: float = 0,
        diag_reg: Optional[float] = 1e-6,
        eps: float = 1e-8,
    ):
        super().__init__()
        if filter_type not in ['pmwf', 'mvdr_souden']:
            raise ValueError(f'Unknown filter type {filter_type}')

        self.filter_type = filter_type
        if self.filter_type == 'mvdr_souden' and filter_beta != 0:
            logging.warning(
                'Using filter type %s: beta will be automatically set to zero (current beta %f) and rank to one (current rank %s).',
                self.filter_type,
                filter_beta,
                filter_rank,
            )
            filter_beta = 0.0
            filter_rank = 'one'
        # Prepare filter
        self.filter = ParametricMultichannelWienerFilter(
            beta=filter_beta,
            rank=filter_rank,
            postfilter=filter_postfilter,
            ref_channel=ref_channel,
            ref_hard=ref_hard,
            ref_hard_use_grad=ref_hard_use_grad,
            ref_subband_weighting=ref_subband_weighting,
            num_subbands=num_subbands,
            output_channel_reduction=filter_output_channel_reduction,
            diag_reg=diag_reg,
            eps=eps,
        )
        # Mask thresholding
        assert mask_min_db < mask_max_db
        self.mask_min = db2mag(mask_min_db)
        self.mask_max = db2mag(mask_max_db)
        # Postmask thresholding
        assert postmask_min_db <= postmask_max_db
        self.postmask_min = db2mag(postmask_min_db)
        self.postmask_max = db2mag(postmask_max_db)

        logging.debug('Initialized %s', self.__class__.__name__)
        logging.debug('\tfilter_type:  %s', self.filter_type)
        logging.debug('\tmask_min:     %e', self.mask_min)
        logging.debug('\tmask_max:     %e', self.mask_max)
        logging.debug('\tpostmask_min: %e', self.postmask_min)
        logging.debug('\tpostmask_max: %e', self.postmask_max)

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "mask": NeuralType(('B', 'C', 'D', 'T'), FloatType()),
            "mask_undesired": NeuralType(('B', 'C', 'D', 'T'), FloatType(), optional=True),
            "input_length": NeuralType(('B',), LengthsType(), optional=True),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "output_length": NeuralType(('B',), LengthsType(), optional=True),
        }

    @typecheck()
    def forward(
        self,
        input: torch.Tensor,
        mask: torch.Tensor,
        mask_undesired: Optional[torch.Tensor] = None,
        input_length: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Apply a mask-based beamformer to the input spectrogram.
        This can be used to generate multi-channel output.
        If `mask` has multiple channels, a multichannel filter is created for each mask,
        and the output is concatenation of individual outputs along the channel dimension.
        The total number of outputs is `num_masks * M`, where `M` is the number of channels
        at the filter output.

        Args:
            input: Input signal complex-valued spectrogram, shape (B, C, F, N)
            mask: Mask for M output signals, shape (B, num_masks, F, N)
            input_length: Length of valid entries along the time dimension, shape (B,)
        
        Returns:
            Multichannel output signal complex-valued spectrogram, shape (B, num_masks * M, F, N)
        """
        # Length mask
        if input_length is not None:
            length_mask: torch.Tensor = make_seq_mask_like(
                lengths=input_length, like=mask[:, 0, ...], time_dim=-1, valid_ones=False
            )

        # Use each mask to generate an output
        output, num_masks = [], mask.size(1)
        for m in range(num_masks):
            # Desired signal mask
            mask_d = mask[:, m, ...]
            # Undesired signal mask
            if mask_undesired is not None:
                mask_u = mask_undesired[:, m, ...]
            elif num_masks == 1:
                # If a single mask is estimated, use the complement
                mask_u = 1 - mask_d
            else:
                # Use sum of all other sources
                mask_u = torch.sum(mask, dim=1) - mask_d

            # Threshold masks
            mask_d = torch.clamp(mask_d, min=self.mask_min, max=self.mask_max)
            mask_u = torch.clamp(mask_u, min=self.mask_min, max=self.mask_max)

            if input_length is not None:
                mask_d = mask_d.masked_fill(length_mask, 0.0)
                mask_u = mask_u.masked_fill(length_mask, 0.0)

            # Apply filter
            output_m = self.filter(input=input, mask_s=mask_d, mask_n=mask_u)

            # Optional: apply a postmask with different thresholds
            if self.postmask_min < self.postmask_max:
                postmask_m = torch.clamp(mask[:, m, ...], min=self.postmask_min, max=self.postmask_max)
                output_m = output_m * postmask_m.unsqueeze(1)

            # Save the current output (B, M, F, T)
            output.append(output_m)

        # Combine outputs along the channel dimension
        # Each output is (B, M, F, T)
        output = torch.concatenate(output, axis=1)

        # Apply masking
        if input_length is not None:
            output = output.masked_fill(length_mask[:, None, ...], 0.0)

        return output, input_length


class WPEFilter(NeuralModule):
    """A weighted prediction error filter.
    Given input signal, and expected power of the desired signal, this
    class estimates a multiple-input multiple-output prediction filter
    and returns the filtered signal. Currently, estimation of statistics
    and processing is performed in batch mode.

    Args:
        filter_length: Length of the prediction filter in frames, per channel
        prediction_delay: Prediction delay in frames
        diag_reg: Diagonal regularization for the correlation matrix Q, applied as diag_reg * trace(Q) + eps
        eps: Small positive constant for regularization

    References:
        - Yoshioka and Nakatani, Generalization of Multi-Channel Linear Prediction
            Methods for Blind MIMO Impulse Response Shortening, 2012
        - Jukić et al, Group sparsity for MIMO speech dereverberation, 2015
    """

    def __init__(self, filter_length: int, prediction_delay: int, diag_reg: Optional[float] = 1e-6, eps: float = 1e-8):
        super().__init__()
        self.filter_length = filter_length
        self.prediction_delay = prediction_delay
        self.diag_reg = diag_reg
        self.eps = eps

        logging.debug('Initialized %s', self.__class__.__name__)
        logging.debug('\tfilter_length:    %d', self.filter_length)
        logging.debug('\tprediction_delay: %d', self.prediction_delay)
        logging.debug('\tdiag_reg:         %g', self.diag_reg)
        logging.debug('\teps:              %g', self.eps)

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "power": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "input_length": NeuralType(('B',), LengthsType(), optional=True),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "output_length": NeuralType(('B',), LengthsType(), optional=True),
        }

    @typecheck()
    def forward(
        self, input: torch.Tensor, power: torch.Tensor, input_length: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Given input and the predicted power for the desired signal, estimate
        the WPE filter and return the processed signal.

        Args:
            input: Input signal, shape (B, C, F, N)
            power: Predicted power of the desired signal, shape (B, C, F, N)
            input_length: Optional, length of valid frames in `input`. Defaults to `None`

        Returns:
            Tuple of (processed_signal, output_length). Processed signal has the same
            shape as the input signal (B, C, F, N), and the output length is the same
            as the input length.
        """
        # Temporal weighting: average power over channels, shape (B, F, N)
        weight = torch.mean(power, dim=-3)
        # Use inverse power as the weight
        weight = 1 / (weight + self.eps)

        # Multi-channel convolution matrix for each subband
        tilde_input = self.convtensor(input, filter_length=self.filter_length, delay=self.prediction_delay)

        # Estimate correlation matrices
        Q, R = self.estimate_correlations(
            input=input, weight=weight, tilde_input=tilde_input, input_length=input_length
        )

        # Estimate prediction filter
        G = self.estimate_filter(Q=Q, R=R)

        # Apply prediction filter
        undesired_signal = self.apply_filter(filter=G, tilde_input=tilde_input)

        # Dereverberation
        desired_signal = input - undesired_signal

        if input_length is not None:
            # Mask padded frames
            length_mask: torch.Tensor = make_seq_mask_like(
                lengths=input_length, like=desired_signal, time_dim=-1, valid_ones=False
            )
            desired_signal = desired_signal.masked_fill(length_mask, 0.0)

        return desired_signal, input_length

    @classmethod
    def convtensor(
        cls, x: torch.Tensor, filter_length: int, delay: int = 0, n_steps: Optional[int] = None
    ) -> torch.Tensor:
        """Create a tensor equivalent of convmtx_mc for each example in the batch.
        The input signal tensor `x` has shape (B, C, F, N).
        Convtensor returns a view of the input signal `x`.

        Note: We avoid reshaping the output to collapse channels and filter taps into
        a single dimension, e.g., (B, F, N, -1). In this way, the output is a view of the input,
        while an additional reshape would result in a contiguous array and more memory use.

        Args:
            x: input tensor, shape (B, C, F, N)
            filter_length: length of the filter, determines the shape of the convolution tensor
            delay: delay to add to the input signal `x` before constructing the convolution tensor
            n_steps: Optional, number of time steps to keep in the out. Defaults to the number of
                    time steps in the input tensor.

        Returns:
            Return a convolutional tensor with shape (B, C, F, n_steps, filter_length)
        """
        if x.ndim != 4:
            raise RuntimeError(f'Expecting a 4-D input. Received input with shape {x.shape}')

        B, C, F, N = x.shape

        if n_steps is None:
            # Keep the same length as the input signal
            n_steps = N

        # Pad temporal dimension
        x = torch.nn.functional.pad(x, (filter_length - 1 + delay, 0))

        # Build Toeplitz-like matrix view by unfolding across time
        tilde_X = x.unfold(-1, filter_length, 1)

        # Trim to the set number of time steps
        tilde_X = tilde_X[:, :, :, :n_steps, :]

        return tilde_X

    @classmethod
    def permute_convtensor(cls, x: torch.Tensor) -> torch.Tensor:
        """Reshape and permute columns to convert the result of
        convtensor to be equal to convmtx_mc. This is used for verification
        purposes and it is not required to use the filter.

        Args:
            x: output of self.convtensor, shape (B, C, F, N, filter_length)

        Returns:
            Output has shape (B, F, N, C*filter_length) that corresponds to
            the layout of convmtx_mc.
        """
        B, C, F, N, filter_length = x.shape

        # .view will not work, so a copy will have to be created with .reshape
        # That will result in more memory use, since we don't use a view of the original
        # multi-channel signal
        x = x.permute(0, 2, 3, 1, 4)
        x = x.reshape(B, F, N, C * filter_length)

        permute = []
        for m in range(C):
            permute[m * filter_length : (m + 1) * filter_length] = m * filter_length + np.flip(
                np.arange(filter_length)
            )
        return x[..., permute]

    def estimate_correlations(
        self,
        input: torch.Tensor,
        weight: torch.Tensor,
        tilde_input: torch.Tensor,
        input_length: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor]:
        """
        Args:
            input: Input signal, shape (B, C, F, N)
            weight: Time-frequency weight, shape (B, F, N)
            tilde_input: Multi-channel convolution tensor, shape (B, C, F, N, filter_length)
            input_length: Length of each input example, shape (B)

        Returns:
            Returns a tuple of correlation matrices for each batch.

            Let `X` denote the input signal in a single subband,
            `tilde{X}` the corresponding multi-channel correlation matrix,
            and `w` the vector of weights.

            The first output is
                Q = tilde{X}^H * diag(w) * tilde{X}     (1)
            for each (b, f).
            The matrix calculated in (1) has shape (C * filter_length, C * filter_length)
            The output is returned in a tensor with shape (B, F, C, filter_length, C, filter_length).

            The second output is
                R = tilde{X}^H * diag(w) * X            (2)
            for each (b, f).
            The matrix calculated in (2) has shape (C * filter_length, C)
            The output is returned in a tensor with shape (B, F, C, filter_length, C). The last
            dimension corresponds to output channels.
        """
        if input_length is not None:
            # Take only valid samples into account
            length_mask: torch.Tensor = make_seq_mask_like(
                lengths=input_length, like=weight, time_dim=-1, valid_ones=False
            )
            weight = weight.masked_fill(length_mask, 0.0)

        # Calculate (1)
        # result: (B, F, C, filter_length, C, filter_length)
        Q = torch.einsum('bjfik,bmfin->bfjkmn', tilde_input.conj(), weight[:, None, :, :, None] * tilde_input)

        # Calculate (2)
        # result: (B, F, C, filter_length, C)
        R = torch.einsum('bjfik,bmfi->bfjkm', tilde_input.conj(), weight[:, None, :, :] * input)

        return Q, R

    def estimate_filter(self, Q: torch.Tensor, R: torch.Tensor) -> torch.Tensor:
        """Estimate the MIMO prediction filter as
            G(b,f) = Q(b,f) \ R(b,f)
        for each subband in each example in the batch (b, f).

        Args:
            Q: shape (B, F, C, filter_length, C, filter_length)
            R: shape (B, F, C, filter_length, C)

        Returns:
            Complex-valued prediction filter, shape (B, C, F, C, filter_length)
        """
        B, F, C, filter_length, _, _ = Q.shape
        assert (
            filter_length == self.filter_length
        ), f'Shape of Q {Q.shape} is not matching filter length {self.filter_length}'

        # Reshape to analytical dimensions for each (b, f)
        Q = Q.reshape(B, F, C * self.filter_length, C * filter_length)
        R = R.reshape(B, F, C * self.filter_length, C)

        # Diagonal regularization
        if self.diag_reg:
            # Regularization: diag_reg * trace(Q) + eps
            diag_reg = self.diag_reg * torch.diagonal(Q, dim1=-2, dim2=-1).sum(-1).real + self.eps
            # Apply regularization on Q
            Q = Q + torch.diag_embed(diag_reg.unsqueeze(-1) * torch.ones(Q.shape[-1], device=Q.device))

        # Solve for the filter
        # G = torch.linalg.solve(Q, R)
        # Use Cholesky decomposition
        QL = torch.linalg.cholesky(Q)
        # Forward and backward substitution
        G = torch.linalg.solve_triangular(QL, R, upper=False)
        G = torch.linalg.solve_triangular(QL.conj().transpose(-2, -1), G, upper=True)

        # Reshape to desired representation: (B, F, input channels, filter_length, output channels)
        G = G.reshape(B, F, C, filter_length, C)
        # Move output channels to front: (B, output channels, F, input channels, filter_length)
        G = G.permute(0, 4, 1, 2, 3)

        return G

    def apply_filter(
        self, filter: torch.Tensor, input: Optional[torch.Tensor] = None, tilde_input: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Apply a prediction filter `filter` on the input `input` as

            output(b,f) = tilde{input(b,f)} * filter(b,f)

        If available, directly use the convolution matrix `tilde_input`.

        Args:
            input: Input signal, shape (B, C, F, N)
            tilde_input: Convolution matrix for the input signal, shape (B, C, F, N, filter_length)
            filter: Prediction filter, shape (B, C, F, C, filter_length)

        Returns:
            Multi-channel signal obtained by applying the prediction filter on
            the input signal, same shape as input (B, C, F, N)
        """
        if input is None and tilde_input is None:
            raise RuntimeError(f'Both inputs cannot be None simultaneously.')
        if input is not None and tilde_input is not None:
            raise RuntimeError(f'Both inputs cannot be provided simultaneously.')

        if tilde_input is None:
            tilde_input = self.convtensor(input, filter_length=self.filter_length, delay=self.prediction_delay)

        # For each (batch, output channel, f, time step), sum across (input channel, filter tap)
        output = torch.einsum('bjfik,bmfjk->bmfi', tilde_input, filter)

        return output


class MaskBasedDereverbWPE(NeuralModule):
    """Multi-channel linear prediction-based dereverberation using
    weighted prediction error for filter estimation.

    An optional mask to estimate the signal power can be provided.
    If a time-frequency mask is not provided, the algorithm corresponds
    to the conventional WPE algorithm.

    Args:
        filter_length: Length of the convolutional filter for each channel in frames.
        prediction_delay: Delay of the input signal for multi-channel linear prediction in frames.
        num_iterations: Number of iterations for reweighting
        mask_min_db: Threshold mask to a minimal value before applying it, defaults to -200dB
        mask_max_db: Threshold mask to a minimal value before applying it, defaults to 0dB
        diag_reg: Diagonal regularization for WPE
        eps: Small regularization constant
        dtype: Data type for internal computations

    References:
        - Kinoshita et al, Neural network-based spectrum estimation for online WPE dereverberation, 2017
        - Yoshioka and Nakatani, Generalization of Multi-Channel Linear Prediction Methods for Blind MIMO Impulse Response Shortening, 2012
    """

    def __init__(
        self,
        filter_length: int,
        prediction_delay: int,
        num_iterations: int = 1,
        mask_min_db: float = -200,
        mask_max_db: float = 0,
        diag_reg: Optional[float] = 1e-6,
        eps: float = 1e-8,
        dtype: torch.dtype = torch.cdouble,
    ):
        super().__init__()
        # Filter setup
        self.filter = WPEFilter(
            filter_length=filter_length, prediction_delay=prediction_delay, diag_reg=diag_reg, eps=eps
        )
        self.num_iterations = num_iterations
        # Mask thresholding
        self.mask_min = db2mag(mask_min_db)
        self.mask_max = db2mag(mask_max_db)
        # Internal calculations
        assert dtype in [torch.cfloat, torch.cdouble], f'Unsupported dtype {dtype}, expecting cfloat or cdouble'
        self.dtype = dtype

        logging.debug('Initialized %s', self.__class__.__name__)
        logging.debug('\tnum_iterations: %s', self.num_iterations)
        logging.debug('\tmask_min:       %g', self.mask_min)
        logging.debug('\tmask_max:       %g', self.mask_max)
        logging.debug('\tdtype:          %s', self.dtype)

    @property
    def input_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "input": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "input_length": NeuralType(('B',), LengthsType(), optional=True),
            "mask": NeuralType(('B', 'C', 'D', 'T'), FloatType(), optional=True),
        }

    @property
    def output_types(self) -> Dict[str, NeuralType]:
        """Returns definitions of module output ports.
        """
        return {
            "output": NeuralType(('B', 'C', 'D', 'T'), SpectrogramType()),
            "output_length": NeuralType(('B',), LengthsType(), optional=True),
        }

    @typecheck()
    def forward(
        self, input: torch.Tensor, input_length: Optional[torch.Tensor] = None, mask: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Given an input signal `input`, apply the WPE dereverberation algoritm.

        Args:
            input: C-channel complex-valued spectrogram, shape (B, C, F, T)
            input_length: Optional length for each signal in the batch, shape (B,)
            mask: Optional mask, shape (B, 1, F, N) or (B, C, F, T)

        Returns:
            Processed tensor with the same number of channels as the input,
            shape (B, C, F, T).
        """
        io_dtype = input.dtype

        with torch.cuda.amp.autocast(enabled=False):
            output = input.to(dtype=self.dtype)

            assert output.is_complex(), f'Expecting complex input, got {input.dtype}'

            for i in range(self.num_iterations):
                magnitude = torch.abs(output)
                if i == 0 and mask is not None:
                    # Apply thresholds
                    mask = torch.clamp(mask, min=self.mask_min, max=self.mask_max)
                    # Mask magnitude
                    magnitude = mask * magnitude
                # Calculate power
                power = magnitude ** 2
                # Apply filter
                output, output_length = self.filter(input=output, input_length=input_length, power=power)

        return output.to(io_dtype), output_length
