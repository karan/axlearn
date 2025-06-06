# Copyright © 2023 Apple Inc.
#
# tensorflow/lingvo:
# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Audio frontends for feature extraction."""

import functools
from collections.abc import Sequence
from typing import Callable, Optional, Protocol

import jax.numpy as jnp

from axlearn.audio.frontend_utils import (
    WindowType,
    frame,
    frame_paddings,
    linear_to_log_mel_spectrogram,
    linear_to_mel_weight_matrix,
    magnitude_spectrogram,
    ms_to_samples,
    next_power_of_2,
    num_frames,
    pre_emphasis,
    windowing,
)
from axlearn.common import ein_ops
from axlearn.common.base_layer import BaseLayer
from axlearn.common.config import (
    REQUIRED,
    InstantiableConfig,
    Required,
    config_class,
    config_for_function,
    maybe_instantiate,
    maybe_set_config,
)
from axlearn.common.module import Module, nowrap
from axlearn.common.utils import Tensor


class StageFn(Protocol):
    """A frontend "stage" is a callable that takes a Tensor and emits a Tensor."""

    def __call__(self, x: Tensor, **kwargs) -> Tensor:
        pass


def normalize_by_mean_std(
    x: Tensor, *, mean: Optional[Sequence[float]] = None, std: Optional[Sequence[float]] = None
) -> Tensor:
    """Scales the input by subtracting pre-computed `mean` and/or dividing by pre-computed `std`."""
    if mean is not None:
        x = x - jnp.array(mean, dtype=x.dtype)
    if std is not None:
        x = x / jnp.maximum(jnp.array(std, dtype=x.dtype), jnp.finfo(x.dtype).eps)
    return x


class BaseFrontend(BaseLayer):
    """Defines the interface for speech frontend."""

    @config_class
    class Config(BaseLayer.Config):
        """Configures BaseFrontend."""

        # Number of output channels.
        output_dim: Required[int] = REQUIRED
        # Number of input samples per second, e.g., 24000 for 24KHz inputs.
        sample_rate: Required[int] = REQUIRED
        # Size of each frame in ms.
        frame_size_ms: Required[float] = REQUIRED
        # Hop size in ms.
        hop_size_ms: Required[float] = REQUIRED

    def __init__(self, cfg: Config, *, parent: Optional[Module]):
        super().__init__(cfg, parent=parent)
        cfg = self.config

        frame_size = ms_to_samples(cfg.frame_size_ms, sample_rate=cfg.sample_rate)
        hop_size = ms_to_samples(cfg.hop_size_ms, sample_rate=cfg.sample_rate)
        self._frame_size = frame_size
        self._hop_size = hop_size


def _log_mel_spectrogram(
    *,
    num_filters: int,
    sample_rate: int,
    fft_size: int,
    mel_floor: float,
    lower_edge_hertz: float = 125.0,
    upper_edge_hertz: Optional[float] = None,
) -> StageFn:
    """Returns a StageFn that computes Log Mel spectrogram."""
    if upper_edge_hertz is None:
        # When sample_rate=16k, upper_edge_hertz=7600 like Lingvo.
        # If your filterbank has bins or filters close to Nyquist (e.g., a triangle filter peaking
        # at 7900–8100 Hz), any response beyond 8000 Hz will wrap around and alias back into
        # the signal.
        # Reducing the upper edge to ~95% of Nyquist (e.g., 7600 Hz) creates a safety margin.
        upper_edge_hertz = 0.95 * (sample_rate // 2)

    # Mel filterbank, used to convert magnitude spectrogram to mel spectrogram. Only needs to be
    # constructed once.
    filterbank = linear_to_mel_weight_matrix(
        num_filters=num_filters,
        num_spectrogram_bins=fft_size // 2 + 1,
        sample_rate=sample_rate,
        lower_edge_hertz=lower_edge_hertz,
        upper_edge_hertz=upper_edge_hertz,
    )

    def fn(fft: Tensor, *, dtype: jnp.dtype) -> Tensor:
        # [batch_size, num_frames, fft_size // 2 + 1].
        spectrogram = magnitude_spectrogram(fft, dtype=dtype)
        # Convert to log-mel. [batch, num_frames, num_filters].
        return linear_to_log_mel_spectrogram(
            spectrogram,
            weight_matrix=filterbank,
            mel_floor=mel_floor,
        )

    return fn


def _pre_emphasis(coeff: float) -> StageFn:
    """Returns a StageFn that applies pre-emphasis."""
    return functools.partial(pre_emphasis, coeff=jnp.array(coeff))


def _fft_dtype(input_dtype: jnp.dtype) -> jnp.dtype:
    if input_dtype in (jnp.bfloat16, jnp.float32, jnp.float64):
        return input_dtype
    elif input_dtype == jnp.int16:
        return jnp.bfloat16
    elif input_dtype == jnp.int32:
        return jnp.float32
    elif input_dtype == jnp.int64:
        return jnp.float64
    else:
        raise ValueError(f"{input_dtype=} is not supported.")


def _cast_for_rfft(x: Tensor) -> Tensor:
    # jnp.fft.rfft input must be float32 or float64.
    if x.dtype in (jnp.float32, jnp.float64):
        return x
    else:
        return x.astype(jnp.float32)


class LogMelFrontend(BaseFrontend):
    """Computes Log Mel spectrogram features.

    The frontend implements the following stages:
        `Framer -> PreEmphasis -> Window -> FFT -> FilterBank -> MeanStdDev`.
    """

    @config_class
    class Config(BaseFrontend.Config):
        """Configures LogMelFrontend."""

        # Number of filters/bands in the output spectrogram.
        num_filters: Required[int] = REQUIRED
        # Number of output channels. Should always be 1.
        output_dim: int = 1
        # Optional output transformation. See `normalize_by_mean_std` for an example.
        output_transformation: Optional[InstantiableConfig[StageFn]] = None
        # Floor of melfilter bank energy to prevent log(0).
        # Recommend to set to 1e-6 or smaller to capture low-energy signals.
        # TODO(markblee): Deprecate this in favor of setting `mel_floor` on `spectrogram`.
        mel_floor: Required[float] = REQUIRED
        # Pre-emphasis filter. If None, skips pre-emphasis.
        pre_emphasis: Optional[InstantiableConfig[StageFn]] = config_for_function(
            _pre_emphasis
        ).set(coeff=0.97)
        # Computes fft size from frame size.
        fft_size: Callable[[int], int] = next_power_of_2
        # Optional customized FFT implementation. Use `jnp.fft.fft` if None.
        # This can be used to support a sharded implementation of FFT.
        # See `sharded_fft` for an example.
        fft: Optional[InstantiableConfig[StageFn]] = None
        # Constructs mel spectogram from FFT outputs.
        spectrogram: InstantiableConfig[StageFn] = config_for_function(_log_mel_spectrogram)

    def __init__(self, cfg: Config, *, parent: Optional[Module]):
        super().__init__(cfg, parent=parent)
        cfg = self.config
        if cfg.output_dim != 1:
            raise ValueError(
                "output_dim should always be 1. Did you mean to configure num_filters instead?"
            )
        self._output_transformation = None
        if cfg.output_transformation is not None:
            self._output_transformation = maybe_instantiate(cfg.output_transformation)

        fft_size = cfg.fft_size(self._frame_size)
        if cfg.fft is not None:
            self._fft = cfg.fft.set(n=fft_size).instantiate()
        else:
            self._fft = lambda x: jnp.fft.rfft(_cast_for_rfft(x), n=fft_size)

        spectrogram = maybe_set_config(
            cfg.spectrogram,
            fft_size=fft_size,
            num_filters=cfg.num_filters,
            sample_rate=cfg.sample_rate,
            mel_floor=cfg.mel_floor,
        )
        self._spectrogram = spectrogram.instantiate()

        self._pre_emphasis = None
        if cfg.pre_emphasis is not None:
            self._frame_size += 1
            self._pre_emphasis = cfg.pre_emphasis.instantiate()

    def forward(self, inputs: Tensor, *, paddings: Tensor) -> dict[str, Tensor]:
        """Computes log-mel spectrogram features.

        Args:
            inputs: Tensor of dtype float32 and shape [batch, seq_len].
            paddings: A 0/1 Tensor of shape [batch, seq_len]. 1's represent padded positions.

        Returns:
            A dict containing:
            - outputs: A Tensor of shape [batch, num_frames, num_filters, 1].
            - paddings: A 0/1 Tensor of shape [batch, num_frames].
        """
        # TODO(markblee): Make these configurable as needed.
        frames = frame(inputs, frame_size=self._frame_size, hop_size=self._hop_size)
        # TODO(dhwang2): Currently, a partial frame is padded. Explore it later.
        out_paddings = frame_paddings(
            paddings,
            frame_size=self._frame_size,
            hop_size=self._hop_size,
        )
        return self._to_logmel(frames, frames_paddings=out_paddings)

    def _to_logmel(self, frames: Tensor, *, frames_paddings: Tensor) -> dict[str, Tensor]:
        """Computes log-mel spectrogram features.

        Args:
            frames: Tensor of shape [batch, num_frames, frame_size].
            frames_paddings: A 0/1 Tensor of shape [batch, num_frames].

        Returns:
            A dict containing:
            - outputs: A Tensor of shape [batch, num_frames, num_filters, 1].
            - paddings: A 0/1 Tensor of shape [batch, num_frames].
        """
        if self._pre_emphasis is not None:
            frames = self._pre_emphasis(frames)
        # Windowing. Defaults to a Hann window.
        # [batch_size, num_frames, frame_size].
        frames = windowing(frames, window_type=WindowType.HANN)
        # FFT and construct spectrogram.
        # [batch_size, num_frames, fft_size] -> [batch, num_frames, num_filters].
        outputs = self._spectrogram(self._fft(frames), dtype=_fft_dtype(frames.dtype))
        if self._output_transformation is not None:
            outputs = self._output_transformation(outputs)
        outputs = outputs * (1 - ein_ops.rearrange(frames_paddings, "b t -> b t 1"))
        return dict(
            outputs=ein_ops.rearrange(outputs, "b t f -> b t f 1"), paddings=frames_paddings
        )

    @nowrap
    def output_shape(self, *, input_shape: Sequence[Optional[int]]):
        """Computes the output shape given input shape.

        Args:
            input_shape: Values for the input dimensions [batch_size, seq_len]. Each value can be an
                integer or None, where None can be used if the dimension is not known.

        Returns:
            The output shape. The dimensions are [batch_size, num_frames, num_filters, 1].

        Raises:
            ValueError: If `input_shape` is invalid.
        """
        cfg: LogMelFrontend.Config = self.config
        if len(input_shape) != 2:
            raise ValueError(f"We expect len(input_shape) = 2, but got {len(input_shape)}.")
        batch_size, seq_len = input_shape
        if seq_len is not None:
            frame_len = num_frames(seq_len, frame_size=self._frame_size, hop_size=self._hop_size)
        else:
            frame_len = None
        return [batch_size, frame_len, cfg.num_filters, cfg.output_dim]
