# Copyright (c) 2017 The Khronos Group Inc.
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

from __future__ import division, print_function, absolute_import

from typing import Optional, List, Tuple, Callable, Any

import numpy as np
import torch
import torch.nn.functional as F

from nnef_tools.core import utils
from nnef_tools.shape_inference import shape_inference


# Helpers

def _expand_to_rank(input, rank):
    # type: (torch.Tensor, int)->torch.Tensor
    rank_diff = rank - len(input.shape)
    return input.reshape(*(tuple(input.shape) + rank_diff * (1,)))


def _expand_binary(input1, input2):
    # type: (torch.Tensor, torch.Tensor)->Tuple[torch.Tensor, torch.Tensor]
    rank = max(len(input1.shape), len(input2.shape))
    return _expand_to_rank(input1, rank), _expand_to_rank(input2, rank)


def _binary(f):
    def g(x, y):
        x, y = _expand_binary(x, y)
        return f(x, y)

    return g


# Operations

def _positive_pad(input, padding, border='constant', value=0.0):
    # type: (torch.Tensor, List[Tuple[int, int]], str, float)->torch.Tensor

    assert all(p >= 0 and q >= 0 for p, q in padding), "Negative padding is not supported "

    assert padding
    assert len(input.shape) in (3, 4, 5)
    assert padding[:2] == [(0, 0), (0, 0)]
    assert border in ("constant", "reflect", "replicate")

    pad = []
    for p, q in reversed(padding[2:]):
        pad += [p, q]

    return F.pad(input=input, pad=pad, mode=border, value=value)


def nnef_pad(input, padding, border='constant', value=0.0):
    # type: (torch.Tensor, List[Tuple[int, int]], str, float)->torch.Tensor

    if not padding:
        raise utils.NNEFToolsException("nnef_pad does not support empty list as padding")

    if len(input.shape) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "nnef_pad is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input.shape)))

    if padding[:2] != [(0, 0), (0, 0)]:
        raise utils.NNEFToolsException(
            "Padding is not implemented in N, C dimensions, given: {}.".format(padding))

    if border not in ("constant", "reflect", "replicate"):
        raise utils.NNEFToolsException(
            "Padding is only implemented with constant, reflect and replicate border, given: {}.".format(border))

    input = _positive_pad(input,
                          padding=[(p if p > 0 else 0, q if q > 0 else 0) for p, q in padding],
                          border=border,
                          value=value)

    return nnef_slice(input,
                      axes=list(range(len(input.shape))),
                      begin=[-p if p < 0 else 0 for p, _q in padding],
                      end=[q if q < 0 else 0 for _p, q in padding])


nnef_add = _binary(lambda x, y: x + y)


def nnef_add_n(values):
    return nnef_add(values[0], nnef_add_n(values[1:])) if len(values) > 1 else values[0]


def nnef_conv(input,  # type: torch.Tensor
              filter,  # type: torch.Tensor
              bias,  # type: torch.Tensor
              border='constant',  # type: str
              padding=None,  # type: Optional[List[Tuple[int, int]]]
              stride=None,  # type: Optional[List[int]]
              dilation=None,  # type: Optional[List[int]]
              groups=1,  # type: int
              ):
    # type: (...)->torch.Tensor

    if len(input.shape) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "Convolution is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input.shape)))

    bias = bias.reshape(1, 1).expand((1, filter.shape[0])) if utils.product(bias.size()) == 1 else bias

    spatial_dims = len(input.shape[2:])
    groups = input.shape[1] if groups == 0 else groups
    stride = [1] * spatial_dims if not stride else stride
    dilation = [1] * spatial_dims if not dilation else dilation
    padding = shape_inference.same_padding(upscaled_input=input.shape[2:],
                                           filter=filter.shape[2:],
                                           stride=stride,
                                           dilation=dilation) if not padding else padding

    pad = nnef_pad(input=input, padding=[(0, 0)] * 2 + padding, border=border)
    conv = {1: F.conv1d, 2: F.conv2d, 3: F.conv3d}[spatial_dims](input=pad,
                                                                 weight=filter,
                                                                 bias=bias.squeeze(dim=0).contiguous(),
                                                                 stride=tuple(stride),
                                                                 padding=0,
                                                                 dilation=tuple(dilation),
                                                                 groups=groups)

    return conv


def nnef_deconv(input,  # type: torch.Tensor
                filter,  # type: torch.Tensor
                bias,  # type: torch.Tensor
                border='constant',  # type: str
                padding=None,  # type: Optional[List[Tuple[int, int]]]
                stride=None,  # type: Optional[List[int]]
                dilation=None,  # type: Optional[List[int]]
                output_shape=None,  # type: Optional[List[int]]
                groups=1,  # type: int
                ):
    # type: (...)->torch.Tensor

    if border != 'constant':
        raise utils.NNEFToolsException("Deconv: '{}' border unsupported.".format(border))

    if output_shape and output_shape[0] != input.shape[0]:
        output_shape = list(output_shape)
        output_shape[0] = input.shape[0]

    rank = len(input.shape)
    if rank not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "Deconvolution is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input.shape)))

    spatial_dims = len(input.shape[2:])
    stride = [1] * spatial_dims if not stride else stride
    dilation = [1] * spatial_dims if not dilation else dilation

    if groups == 0:
        if output_shape:
            groups = output_shape[1]
        else:
            # Planewise deconvolution without output_size, assuming that #(input channels) = #(output channels)
            groups = filter.shape[0]

    output_channels = filter.shape[1] * groups
    if output_shape:
        assert output_shape[1] == output_channels

    if not padding:
        output_size = output_shape[2:] if output_shape else [i * s for i, s in zip(input.shape[2:], stride)]
        padding = shape_inference.same_padding(upscaled_input=output_size,
                                               filter=filter.shape[2:],
                                               stride=stride,
                                               dilation=dilation)
    else:
        output_size = output_shape[2:] if output_shape else shape_inference.conv(input=list(input.shape),
                                                                                 filter=filter.shape[2:],
                                                                                 padding=padding,
                                                                                 stride=stride,
                                                                                 dilation=dilation,
                                                                                 groups=groups,
                                                                                 output_channels=output_channels,
                                                                                 format=shape_inference.Format.NCHW,
                                                                                 deconv=True)[2:]

    uncropped_output_size = shape_inference.conv(input=list(input.shape),
                                                 filter=filter.shape[2:],
                                                 padding=shape_inference.Padding.VALID,
                                                 stride=stride,
                                                 dilation=dilation,
                                                 groups=groups,
                                                 output_channels=output_channels,
                                                 format=shape_inference.Format.NCHW,
                                                 deconv=True)[2:]

    crop_before = [p for p, _q in padding]
    crop_after = [uncropped - out - before
                  for uncropped, out, before
                  in zip(uncropped_output_size, output_size, crop_before)]

    bias = bias.reshape(1, 1).expand((1, output_channels)) if utils.product(bias.size()) == 1 else bias

    deconv = {1: F.conv_transpose1d,
              2: F.conv_transpose2d,
              3: F.conv_transpose3d}[spatial_dims](input=input,
                                                   weight=filter,
                                                   bias=bias.squeeze(dim=0).contiguous(),
                                                   stride=tuple(stride),
                                                   padding=0,
                                                   output_padding=0,
                                                   groups=groups,
                                                   dilation=tuple(dilation))

    return nnef_pad(deconv, padding=[(0, 0), (0, 0)] + [(-cb, -ca) for cb, ca in zip(crop_before, crop_after)])


def _evaluate_max_pool_or_box_params(input_shape, size, padding, stride, dilation):
    rank = len(input_shape)
    stride = [1] * rank if not stride else stride
    dilation = [1] * rank if not dilation else dilation
    padding = shape_inference.same_padding(upscaled_input=input_shape,
                                           filter=size,
                                           stride=stride,
                                           dilation=dilation) if not padding else padding
    return padding, stride, dilation


def _max_pool_impl(input,  # type: torch.Tensor
                   size,  # type: List[int]
                   border='constant',  # type: str
                   padding=None,  # type: Optional[List[Tuple[int, int]]]
                   stride=None,  # type: Optional[List[int]]
                   dilation=None,  # type: Optional[List[int]]
                   with_index=False,  # type: bool
                   ):
    # type: (...)->torch.Tensor

    spatial_dims = len(input.shape) - 2
    value = float('-inf') if border == 'ignore' else 0.0
    border = 'constant' if border == 'ignore' else border

    pad = nnef_pad(input=input, padding=padding, border=border, value=value)

    result = {1: F.max_pool1d, 2: F.max_pool2d, 3: F.max_pool3d}[spatial_dims](input=pad,
                                                                               kernel_size=size[2:],
                                                                               stride=stride[2:],
                                                                               padding=0,
                                                                               dilation=dilation[2:],
                                                                               return_indices=with_index)
    return result


def _box_impl(input,  # type: torch.Tensor
              size,  # type: List[int]
              border,  # type: str
              padding,  # type: List[Tuple[int, int]]
              stride,  # type: List[int]
              dilation,  # type: List[int]
              normalize,  # type: bool
              ):
    # type: (...)->torch.Tensor

    assert 3 <= len(input.shape) <= 5
    assert len(input.shape) == len(size) == len(padding) == len(stride) == len(dilation)
    assert padding[:2] == [(0, 0), (0, 0)]
    assert size[:2] == stride[:2] == dilation[:2]

    if dilation and any(d != 1 for d in dilation):
        raise utils.NNEFToolsException(
            "Box (avg or sum pooling) is only implemented for dilation = 1."
        )

    spatial_dims = len(input.shape) - 2

    pad = nnef_pad(input=input, padding=padding, border='constant' if border == 'ignore' else border)

    avg_pool = {1: F.avg_pool1d, 2: F.avg_pool2d, 3: F.avg_pool3d}[spatial_dims](
        input=pad,
        kernel_size=size[2:],
        stride=stride[2:],
        padding=0)

    if border == 'ignore' and normalize:
        ones = torch.ones_like(input)
        padded_ones = nnef_pad(input=ones, padding=padding, border='constant')
        avg_pool_ones = {1: F.avg_pool1d, 2: F.avg_pool2d, 3: F.avg_pool3d}[spatial_dims](
            input=padded_ones,
            kernel_size=size[2:],
            stride=stride[2:],
            padding=0)
        # If padding is big, zero averages can happen on the border, don't divide by zero
        avg_pool_ones = nnef_select(avg_pool_ones > 0, avg_pool_ones, torch.ones_like(avg_pool_ones))
        avg_pool /= avg_pool_ones

    if normalize:
        return avg_pool
    else:
        return avg_pool * utils.product(size)


def _get_transform_for_box_or_max_pool(input_shape, active):
    # type: (List[int], List[bool])->Any
    assert len(input_shape) >= 3
    assert len(input_shape) == len(active)
    if sum(active) > 3:
        raise utils.NNEFToolsException(
            "Sliding window operations are not supported if they have more than 3 'active' dimensions. "
            "We have: {}".format(sum(active)))
    if 3 <= len(input_shape) <= 5 and not active[0] and not active[1]:  # Direct support
        return None, None, None, None
    else:
        inactive_dims = [i for i, a in enumerate(active) if not a]
        active_dims = [i for i, a in enumerate(active) if a]
        inactive_shape = [s for i, s in enumerate(input_shape) if i not in active_dims]
        active_shape = [s for i, s in enumerate(input_shape) if i in active_dims]
        perm = inactive_dims + active_dims
        perm_inv = utils.inverse_permutation(perm)
    return perm, perm_inv, inactive_shape, active_shape


def _box_or_max_pool(input,  # type: torch.Tensor
                     size,  # type: List[int]
                     border='constant',  # type: str
                     padding=None,  # type: Optional[List[Tuple[int, int]]],
                     stride=None,  # type: Optional[List[int]],
                     dilation=None,  # type: Optional[List[int]]
                     normalize=False,  # type: bool
                     is_max_pool=False,  # type: bool
                     ):
    assert not (normalize and is_max_pool)

    rank = len(input.shape)
    padding, stride, dilation = _evaluate_max_pool_or_box_params(input_shape=list(input.shape),
                                                                 size=size,
                                                                 padding=padding,
                                                                 stride=stride,
                                                                 dilation=dilation)
    active = [size_ != 1 or padding_ != (0, 0) or stride_ != 1 or dilation_ != 1
              for size_, padding_, stride_, dilation_
              in zip(size, padding, stride, dilation)]

    if sum(active) == 0:
        return input

    if rank < 3:
        perm, perm_inv, inactive_shape, active_shape = None, None, None, None
    else:
        perm, perm_inv, inactive_shape, active_shape = _get_transform_for_box_or_max_pool(list(input.shape), active)

    if rank < 3:
        input = input.unsqueeze(0).unsqueeze(0)
        size = [1, 1] + size
        padding = [(0, 0), (0, 0)] + padding
        stride = [1, 1] + stride
        dilation = [1, 1] + dilation
    elif perm is not None:
        input = input.permute(*perm)
        size = utils.apply_permutation(size, perm)
        padding = utils.apply_permutation(padding, perm)
        stride = utils.apply_permutation(stride, perm)
        dilation = utils.apply_permutation(dilation, perm)

        active_rank = len(active_shape)
        input = input.reshape(*[utils.product(inactive_shape), 1] + active_shape)
        size = [1, 1] + size[-active_rank:]
        padding = [(0, 0), (0, 0)] + padding[-active_rank:]
        stride = [1, 1] + stride[-active_rank:]
        dilation = [1, 1] + dilation[-active_rank:]

    if is_max_pool:
        output = _max_pool_impl(
            input=input, size=size, border=border, padding=padding, stride=stride, dilation=dilation, with_index=False)
    else:
        output = _box_impl(input=input,
                           size=size,
                           border=border,
                           padding=padding,
                           stride=stride,
                           dilation=dilation,
                           normalize=normalize)

    if rank < 3:
        output = output.squeeze(0).squeeze(0)
    elif perm is not None:
        active_rank = len(active_shape)
        output = output.reshape(inactive_shape + list(output.shape)[-active_rank:])
        output = output.permute(*perm_inv)

    return output


def nnef_max_pool(input,  # type: torch.Tensor
                  size,  # type: List[int]
                  border='constant',  # type: str
                  padding=None,  # type: Optional[List[Tuple[int, int]]]
                  stride=None,  # type: Optional[List[int]]
                  dilation=None,  # type: Optional[List[int]]
                  ):
    # type: (...)->torch.Tensor
    return _box_or_max_pool(
        input, size=size, border=border, padding=padding, stride=stride, dilation=dilation, is_max_pool=True)


def nnef_max_pool_with_index(input,  # type: torch.Tensor
                             size,  # type: List[int]
                             border='constant',  # type: str
                             padding=None,  # type: Optional[List[Tuple[int, int]]]
                             stride=None,  # type: Optional[List[int]]
                             dilation=None,  # type: Optional[List[int]]
                             ):
    # type: (...)->torch.Tensor

    input_shape = list(input.shape)
    padding, stride, dilation = _evaluate_max_pool_or_box_params(input_shape=input_shape,
                                                                 size=size,
                                                                 padding=padding,
                                                                 stride=stride,
                                                                 dilation=dilation)

    if len(input_shape) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "max_pool_with_index is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input_shape)))

    if size[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "max_pool_with_index is only implemented for size = 1 in N and C dimensions."
        )
    if padding[:2] != [(0, 0), (0, 0)]:
        raise utils.NNEFToolsException(
            "max_pool_with_index is only implemented for padding = (0, 0) in N and C dimensions."
        )
    if stride[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "max_pool_with_index is only implemented for stride = 1 in N and C dimensions."
        )
    if dilation[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "max_pool_with_index is only implemented for dilation = 1 in N and C dimensions."
        )

    return _max_pool_impl(input, size=size, border=border, padding=padding, stride=stride, dilation=dilation,
                          with_index=True)


def nnef_argmax_pool(input,  # type: torch.Tensor
                     size,  # type: List[int]
                     border='constant',  # type: str
                     padding=None,  # type: Optional[List[Tuple[int, int]]]
                     stride=None,  # type: Optional[List[int]]
                     dilation=None,  # type: Optional[List[int]]
                     ):
    # type: (...)->torch.Tensor
    _, index = nnef_max_pool_with_index(
        input, size=size, border=border, padding=padding, stride=stride, dilation=dilation)
    return index


def nnef_box(input,  # type: torch.Tensor
             size,  # type: List[int]
             border='constant',  # type: str
             padding=None,  # type: Optional[List[Tuple[int, int]]]
             stride=None,  # type: Optional[List[int]]
             dilation=None,  # type: Optional[List[int]]
             normalize=False,  # type: bool
             ):
    # type: (...)->torch.Tensor
    return _box_or_max_pool(
        input, size=size, border=border, padding=padding, stride=stride, dilation=dilation, normalize=normalize)


def nnef_debox(input,  # type: torch.Tensor
               size,  # type: List[int]
               border='constant',  # type: str
               padding=None,  # type: Optional[List[Tuple[int, int]]]
               stride=None,  # type: Optional[List[int]]
               dilation=None,  # type: Optional[List[int]]
               output_shape=None,  # type: Optional[List[int]]
               normalize=False,  # type: bool
               ):
    if border not in ('constant', 'ignore'):
        raise utils.NNEFToolsException("Debox: '{}' border unsupported.".format(border))

    if len(size) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "Debox is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(size)))

    if size[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "Debox is only implemented for size = 1 in N and C dimensions."
        )

    if padding and padding[:2] != [(0, 0), (0, 0)]:
        raise utils.NNEFToolsException(
            "Debox is only implemented for padding = (0, 0) in N and C dimensions."
        )
    if stride and stride[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "Debox is only implemented for stride = 1 in N and C dimensions."
        )
    if dilation and dilation[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "Debox is only implemented for dilation = 1 in N and C dimensions."
        )

    filter = torch.full(size=[input.shape[1], 1] + list(size)[2:],
                        fill_value=(1.0 / utils.product(size) if normalize else 1.0),
                        device=input.device,
                        dtype=input.dtype)
    bias = torch.zeros(size=tuple(), device=input.device, dtype=input.dtype)

    return nnef_deconv(input=input,
                       filter=filter,
                       bias=bias,
                       border='constant',
                       padding=padding[2:] if padding else padding,
                       stride=stride[2:] if stride else stride,
                       dilation=dilation[2:] if dilation else dilation,
                       output_shape=output_shape,
                       groups=input.shape[1])


def nnef_avg_pool(input,  # type: torch.Tensor
                  size,  # type: List[int]
                  border='constant',  # type: str
                  padding=None,  # type: Optional[List[Tuple[int, int]]],
                  stride=None,  # type: Optional[List[int]],
                  dilation=None,  # type: Optional[List[int]]
                  ):
    # type: (...)->torch.Tensor
    return nnef_box(input, size=size, border=border, padding=padding, stride=stride, dilation=dilation, normalize=True)


def nnef_rms_pool(input,  # type: torch.Tensor
                  size,  # type: List[int]
                  border='constant',  # type: str
                  padding=None,  # type: Optional[List[Tuple[int, int]]],
                  stride=None,  # type: Optional[List[int]],
                  dilation=None,  # type: Optional[List[int]]
                  ):
    # type: (...)->torch.Tensor
    return torch.sqrt(nnef_avg_pool(torch.pow(input, 2.0),
                                    size=size,
                                    border=border,
                                    padding=padding,
                                    stride=stride,
                                    dilation=dilation))


def nnef_desample(input,  # type: torch.Tensor
                  index,  # type: torch.Tensor
                  size,  # type: List[int]
                  border='constant',  # type: str
                  padding=None,  # type: Optional[List[Tuple[int, int]]]
                  stride=None,  # type: Optional[List[int]]
                  dilation=None,  # type: Optional[List[int]]
                  output_shape=None,  # type: Optional[List[int]]
                  ):
    # type: (...)->torch.Tensor

    if output_shape and output_shape[0] != input.shape[0]:
        output_shape = list(output_shape)
        output_shape[0] = input.shape[0]

    input_shape = list(input.shape)
    rank = len(input_shape)
    spatial_dims = len(input_shape[2:])

    if len(input_shape) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "Desample is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input_shape)))

    if size and size[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "Desample is only implemented for size = 1 in N and C dimensions."
        )
    if padding and padding[:2] != [(0, 0), (0, 0)]:
        raise utils.NNEFToolsException(
            "Desample is only implemented for padding = (0, 0) in N and C dimensions."
        )
    if stride and stride[:2] != [1, 1]:
        raise utils.NNEFToolsException(
            "Desample is only implemented for stride = 1 in N and C dimensions."
        )
    if dilation and not all(d == 1 for d in dilation):
        raise utils.NNEFToolsException(
            "Desample is only implemented for dilation = 1."
        )

    stride = [1] * rank if not stride else stride
    dilation = [1] * rank if not dilation else dilation

    if not padding:
        calculated_output_shape = [i * s for i, s in zip(input_shape, stride)]
        padding = shape_inference.same_padding(upscaled_input=calculated_output_shape,
                                               filter=size,
                                               stride=stride,
                                               dilation=dilation)
    else:
        calculated_output_shape = shape_inference.sliding_window(input=input_shape,
                                                                 filter=size,
                                                                 padding=padding,
                                                                 stride=stride,
                                                                 dilation=dilation,
                                                                 upscale=True)

    output_shape = output_shape if output_shape else calculated_output_shape
    padded_output_shape = [s + p + q for s, (p, q) in zip(output_shape, padding)]
    unpooled = {1: F.max_unpool1d, 2: F.max_unpool2d, 3: F.max_unpool3d}[spatial_dims](
        input=input, indices=index, kernel_size=size[2:], stride=stride[2:], padding=0, output_size=padded_output_shape)
    return nnef_slice(unpooled,
                      axes=list(range(rank)),
                      begin=[p for p, _q in padding],
                      end=[p + s for (p, _q), s in zip(padding, output_shape)])


def nnef_batch_normalization(input,  # type: torch.Tensor
                             mean,  # type: torch.Tensor
                             variance,  # type: torch.Tensor
                             offset,  # type: torch.Tensor
                             scale,  # type: torch.Tensor
                             epsilon,  # type: float
                             is_training=False,  # type: bool
                             momentum=0.1,  # type: float
                             ):
    # type: (...)->torch.Tensor

    if isinstance(mean, torch.nn.Parameter):
        mean.requires_grad = False
    if isinstance(variance, torch.nn.Parameter):
        variance.requires_grad = False

    return F.batch_norm(input=input,
                        running_mean=nnef_squeeze(mean, axes=[0]),
                        running_var=nnef_squeeze(variance, axes=[0]),
                        weight=nnef_squeeze(scale, axes=[0]),
                        bias=nnef_squeeze(offset, axes=[0]),
                        training=is_training,
                        momentum=momentum,
                        eps=epsilon)


def nnef_multilinear_upsample(input, factor, method='symmetric', border='replicate'):
    # type: (torch.Tensor, List[int], str, str)->torch.Tensor

    rank = len(factor)
    if rank not in (1, 2):
        raise utils.NNEFToolsException(
            "Multilinear upsample is only implemented for 1 and 2 spatial dimensions, got: {}.".format(rank))

    assert len(input.shape) == rank + 2

    if factor != [2] * rank:
        raise utils.NNEFToolsException("Multilinear upsample is only supported if factor=2, got: {}".format(factor))

    n, c, = input.shape[:2]

    bias = torch.zeros(size=tuple(), device=input.device, dtype=input.dtype)
    mode = 'linear' if rank == 1 else 'bilinear'

    if method == 'symmetric':
        if border == 'replicate':
            return F.interpolate(input=input, scale_factor=tuple(factor), mode=mode, align_corners=False)

        weights = [0.25, 0.75, 0.75, 0.25] if rank == 1 else [0.0625, 0.1875, 0.1875, 0.0625,
                                                              0.1875, 0.5625, 0.5625, 0.1875,
                                                              0.1875, 0.5625, 0.5625, 0.1875,
                                                              0.0625, 0.1875, 0.1875, 0.0625]
        array = np.array(weights * c, dtype=np.float32).reshape([c, 1] + [4] * rank)
        filter = torch.from_numpy(array).to(device=input.device, dtype=input.dtype)
        return nnef_deconv(input, filter, bias, stride=[2] * rank, padding=[(1, 1)] * rank, border='constant',
                           groups=c, output_shape=[n, c] + [2 * s for s in input.shape[2:]])
    elif method == 'asymmetric':
        if border == 'replicate':
            input = nnef_pad(input, padding=[(0, 0), (0, 0)] + [(1, 0)] * rank, border=border)

        weights = [0.5, 1.0, 0.5] if rank == 1 else [0.25, 0.5, 0.25,
                                                     0.50, 1.0, 0.50,
                                                     0.25, 0.5, 0.25]
        array = np.array(weights * c, dtype=np.float32).reshape([c, 1] + [3] * rank)
        filter = torch.from_numpy(array).to(device=input.device, dtype=input.dtype)
        output = nnef_deconv(input, filter, bias, stride=[2] * rank, padding=[(0, 1)] * rank, border='constant',
                             groups=c, output_shape=[n, c] + [2 * s for s in input.shape[2:]])

        if border == 'replicate':
            output = nnef_slice(output, axes=list(range(2, 2 + rank)), begin=[2] * rank, end=[0] * rank)
        return output
    else:
        return F.interpolate(input=input, scale_factor=tuple(factor), mode=mode, align_corners=True)


def nnef_nearest_upsample(input, factor):
    # type: (torch.Tensor, List[int])->torch.Tensor

    if len(input.shape) not in (3, 4, 5):
        raise utils.NNEFToolsException(
            "Nearest upsample is only implemented for 3D, 4D, 5D tensors, given: {}D.".format(len(input.shape)))

    return F.interpolate(input=input, scale_factor=tuple(factor), mode='nearest')


def nnef_softmax(x, axes=None):
    # type: (torch.Tensor, Optional[List[int]])->torch.Tensor

    axes = [1] if axes is None else axes

    if len(axes) == 0:
        return x
    elif len(axes) == 1:
        return F.softmax(x, dim=axes[0])
    else:
        m = nnef_max_reduce(x, axes=axes)
        e = torch.exp(x - m)
        return e / nnef_sum_reduce(x, axes=axes)


def nnef_local_response_normalization(input, size, alpha=1.0, beta=0.5, bias=1.0):
    # type: (torch.Tensor, List[int], float, float, float)->torch.Tensor

    sigma = bias + alpha * nnef_box(torch.pow(input, 2.0), size=size, normalize=True)
    return input / torch.pow(sigma, beta)


def nnef_local_mean_normalization(input, size):
    # type: (torch.Tensor, List[int])->torch.Tensor
    mean = nnef_box(input, size=size, normalize=True)
    return input - mean


def nnef_local_variance_normalization(input, size, bias=0.0, epsilon=0.0):
    # type: (torch.Tensor, List[int], float, float)->torch.Tensor
    sigma = torch.sqrt(nnef_box(torch.pow(input, 2.0), size=size, normalize=True))
    return input / torch.max(sigma + bias,
                             torch.full(size=[], fill_value=epsilon, device=input.device, dtype=input.dtype))


def nnef_local_contrast_normalization(input, size, bias=0.0, epsilon=0.0):
    # type: (torch.Tensor, List[int], float, float)->torch.Tensor
    centered = nnef_local_mean_normalization(input, size=size)
    return nnef_local_variance_normalization(centered, size=size, bias=bias, epsilon=epsilon)


def nnef_l1_normalization(input, axes, bias=0.0, epsilon=0.0):
    # type: (torch.Tensor, List[int], float, float)->torch.Tensor
    sigma = nnef_sum_reduce(torch.abs(input), axes=axes)
    return input / torch.max(sigma + bias,
                             torch.full(size=[], fill_value=epsilon, device=input.device, dtype=input.dtype))


def nnef_l2_normalization(input, axes, bias=0.0, epsilon=0.0):
    # type: (torch.Tensor, List[int], float, float)->torch.Tensor
    sigma = torch.sqrt(nnef_sum_reduce(torch.pow(input, 2.0), axes=axes))
    return input / torch.max(sigma + bias,
                             torch.full(size=[], fill_value=epsilon, device=input.device, dtype=input.dtype))


def nnef_matmul(A, B, transposeA=False, transposeB=False):
    # type:(torch.Tensor, torch.Tensor, bool, bool)->torch.Tensor

    return torch.matmul(torch.transpose(A, len(A.shape) - 2, len(A.shape) - 1) if transposeA else A,
                        torch.transpose(B, len(B.shape) - 2, len(B.shape) - 1) if transposeB else B)


def nnef_split(value, axis, ratios):
    # type:(torch.Tensor, int, List[int])->torch.Tensor
    assert value.shape[axis] % sum(ratios) == 0

    multiplier = value.shape[axis] // sum(ratios)
    sections = [ratio * multiplier for ratio in ratios]
    return torch.split(value, split_size_or_sections=sections, dim=axis)


def nnef_slice(input, axes, begin, end):
    # type:(torch.Tensor, List[int], List[int], List[int])->torch.Tensor

    shape = list(input.shape)

    for axis, b, e in zip(axes, begin, end):
        if b < 0:
            e += shape[axis]
        if e <= 0:
            e += shape[axis]
        input = input.narrow(dim=axis, start=b, length=(e - b))

    return input


def nnef_select(condition, true_value, false_value):
    # type:(torch.Tensor, torch.Tensor, torch.Tensor)->torch.Tensor
    rank = max(len(condition.shape), len(true_value.shape), len(false_value.shape))
    return torch.where(_expand_to_rank(condition, rank),
                       _expand_to_rank(true_value, rank),
                       _expand_to_rank(false_value, rank))


def _nnef_generic_reduce(input, axes, f):
    # type:(torch.Tensor, List[int], Callable)->torch.Tensor
    if not axes:
        return input
    for axis in reversed(sorted(axes)):
        input = f(input=input, dim=axis, keepdim=True)
    return input


def nnef_sum_reduce(input, axes, normalize=False):
    # type:(torch.Tensor, List[int], bool)->torch.Tensor
    return _nnef_generic_reduce(input=input, axes=axes, f=torch.mean if normalize else torch.sum)


def nnef_max_reduce(input, axes):
    # type:(torch.Tensor, List[int])->torch.Tensor
    return _nnef_generic_reduce(input=input, axes=axes,
                                f=lambda input, dim, keepdim: torch.max(input, dim=dim, keepdim=keepdim)[0])


def nnef_min_reduce(input, axes):
    # type:(torch.Tensor, List[int])->torch.Tensor
    return _nnef_generic_reduce(input=input, axes=axes,
                                f=lambda input, dim, keepdim: torch.min(input, dim=dim, keepdim=keepdim)[0])


def nnef_mean_reduce(input, axes):
    # type:(torch.Tensor, List[int])->torch.Tensor
    return _nnef_generic_reduce(input=input, axes=axes, f=torch.mean)


def _nnef_argminmax_reduce(input, axes, argmin=False):
    # type:(torch.Tensor, List[int], bool)->torch.Tensor
    if len(axes) == 1:
        return _nnef_generic_reduce(input=input, axes=axes, f=torch.argmin if argmin else torch.argmax)
    else:
        axes = sorted(axes)
        consecutive_axes = list(range(axes[0], axes[0] + len(axes)))
        if axes == consecutive_axes:
            reshaped = nnef_reshape(input,
                                    shape=(list(input.shape)[:axes[0]]
                                           + [-1]
                                           + list(input.shape[axes[0] + len(axes):])))
            reduced = _nnef_generic_reduce(input=reshaped, axes=[axes[0]], f=torch.argmin if argmin else torch.argmax)
            reshaped = nnef_reshape(reduced, shape=list(dim if axis not in axes else 1
                                                        for axis, dim in enumerate(input.shape)))
            return reshaped
        else:
            raise utils.NNEFToolsException(
                "{} is only implemented for consecutive axes.".format("argmin_reduce" if argmin else "argmax_reduce"))


def nnef_argmax_reduce(input, axes):
    # type:(torch.Tensor, List[int])->torch.Tensor
    return _nnef_argminmax_reduce(input, axes, argmin=False)


def nnef_argmin_reduce(input, axes):
    # type:(torch.Tensor, List[int])->torch.Tensor
    return _nnef_argminmax_reduce(input, axes, argmin=True)


def nnef_clamp(x, a, b):
    # type:(torch.Tensor, torch.Tensor, torch.Tensor)->torch.Tensor
    rank = max(len(x.shape), len(a.shape), len(b.shape))
    x = _expand_to_rank(x, rank)
    a = _expand_to_rank(a, rank)
    b = _expand_to_rank(b, rank)
    return torch.max(torch.min(x, b), a)


def nnef_nearest_downsample(input, factor):
    # type: (torch.Tensor, List[int])->torch.Tensor
    dims = len(input.shape)
    return nnef_box(input, size=[1] * dims, stride=[1, 1] + factor, padding=[(0, 0)] * dims)


def nnef_area_downsample(input, factor):
    # type: (torch.Tensor, List[int])->torch.Tensor
    dims = len(input.shape)
    return nnef_box(input, size=[1, 1] + factor, stride=[1, 1] + factor, padding=[(0, 0)] * dims, normalize=True)


def nnef_moments(input, axes):
    # type: (torch.Tensor, List[int])->Tuple[torch.Tensor, torch.Tensor]
    mean = nnef_mean_reduce(input, axes=axes)
    variance = nnef_mean_reduce(torch.pow(input - mean, 2.0), axes=axes)
    return mean, variance


def nnef_linear(input, filter, bias):
    # type: (torch.Tensor, torch.Tensor, torch.Tensor)->torch.Tensor
    matmul = nnef_matmul(A=input, B=filter, transposeB=True)
    matmul, bias = _expand_binary(matmul, bias)
    return matmul + bias


def nnef_separable_conv(input,  # type: torch.Tensor
                        plane_filter,  # type: torch.Tensor
                        point_filter,  # type: torch.Tensor
                        bias,  # type: torch.Tensor
                        border='constant',  # type: str
                        padding=None,  # type: Optional[List[Tuple[int, int]]]
                        stride=None,  # type: Optional[List[int]]
                        dilation=None,  # type: Optional[List[int]]
                        groups=1,  # type: int
                        ):
    # type: (...)->torch.Tensor
    filtered = nnef_conv(input, plane_filter,
                         bias=torch.zeros(size=tuple(), device=input.device, dtype=input.dtype),
                         border=border,
                         padding=padding,
                         stride=stride,
                         dilation=dilation,
                         groups=0)
    return nnef_conv(filtered, point_filter, bias, groups=groups)


def nnef_separable_deconv(input,  # type: torch.Tensor
                          plane_filter,  # type: torch.Tensor
                          point_filter,  # type: torch.Tensor
                          bias,  # type: torch.Tensor
                          border='constant',  # type: str
                          padding=None,  # type: Optional[List[Tuple[int, int]]]
                          stride=None,  # type: Optional[List[int]]
                          dilation=None,  # type: Optional[List[int]]
                          output_shape=None,  # type: Optional[List[int]]
                          groups=1,  # type: int
                          ):
    # type: (...)->torch.Tensor
    filtered = nnef_deconv(input,
                           point_filter,
                           torch.zeros(size=tuple(), device=input.device, dtype=input.dtype),
                           groups=groups)
    return nnef_deconv(filtered, plane_filter, bias,
                       border=border,
                       padding=padding,
                       stride=stride,
                       dilation=dilation,
                       output_shape=output_shape,
                       groups=0)


def nnef_copy_n(x, times):
    # type: (torch.Tensor, int)->List[torch.Tensor]
    return [x.clone() for _ in range(times)]


_max = max  # Save it before shadowing it in nnef_linear_quantize


def nnef_linear_quantize(x, min, max, bits):
    # type: (torch.Tensor, torch.Tensor, torch.Tensor, int)->torch.Tensor

    rank = _max(len(x.shape), len(min.shape), len(max.shape))
    x = _expand_to_rank(x, rank)
    min = _expand_to_rank(min, rank)
    max = _expand_to_rank(max, rank)

    r = float(2 ** bits - 1)
    z = nnef_clamp(x, min, max)
    q = torch.round((z - min) / (max - min) * r)
    return q / r * (max - min) + min


def nnef_logarithmic_quantize(x, max, bits):
    # type: (torch.Tensor, torch.Tensor, int)->torch.Tensor

    x, max = _expand_binary(x, max)

    r = float(2 ** bits - 1)
    m = torch.ceil(torch.log2(max))
    q = torch.round(nnef_clamp(torch.log2(torch.abs(x)), m - r, m))
    return torch.sign(x) * torch.pow(2.0, q)


def nnef_reshape(input, shape, axis_start=0, axis_count=-1):
    # type: (torch.Tensor, List[int], int, int)->torch.Tensor

    return input.reshape(shape_inference.reshape(input=list(input.shape),
                                                 shape=shape,
                                                 offset=axis_start,
                                                 count=axis_count,
                                                 zero_means_same=True))


def nnef_update(variable, value):
    # type: (torch.Tensor, torch.Tensor)->torch.Tensor
    return value


def nnef_transpose(input, axes):
    return input.permute(*(axes + list(range(len(axes), len(input.shape)))))


def nnef_squeeze(input, axes):
    return input.reshape(shape_inference.squeeze(input.shape, axes))


def nnef_unsqueeze(input, axes):
    return input.reshape(shape_inference.unsqueeze(input.shape, axes))


"""
The supported operations
"""
operations = {
    'update': nnef_update,
    'reshape': nnef_reshape,
    'transpose': nnef_transpose,
    'concat': lambda values, axis: torch.cat(values, axis),
    'split': nnef_split,
    'slice': nnef_slice,
    'squeeze': nnef_squeeze,
    'unsqueeze': nnef_unsqueeze,
    'stack': lambda values, axis: torch.stack(values, axis),
    'unstack': lambda value, axis: torch.unbind(value, axis),
    'add': nnef_add,
    'add_n': nnef_add_n,
    'sub': _binary(lambda x, y: x - y),
    'mul': _binary(lambda x, y: x * y),
    'div': _binary(lambda x, y: x / y),
    'pow': _binary(torch.pow),
    'exp': torch.exp,
    'log': torch.log,
    'abs': torch.abs,
    'sign': torch.sign,
    'rcp': torch.reciprocal,
    'neg': torch.neg,
    'copy': torch.clone,
    'lt': _binary(lambda x, y: x < y),
    'gt': _binary(lambda x, y: x > y),
    'le': _binary(lambda x, y: x <= y),
    'ge': _binary(lambda x, y: x >= y),
    'eq': _binary(torch.eq),
    'ne': _binary(torch.ne),
    'and': _binary(lambda x, y: x & y),
    'or': _binary(lambda x, y: x | y),
    'not': lambda x: ~x,
    'floor': torch.floor,
    'ceil': torch.ceil,
    'round': torch.round,
    'select': nnef_select,
    'sqr': lambda x: torch.pow(x, 2.0),
    'sqrt': torch.sqrt,
    'rsqr': lambda x: torch.pow(x, -2.0),
    'rsqrt': torch.rsqrt,
    'log2': torch.log2,
    'min': _binary(torch.min),
    'max': _binary(torch.max),
    'clamp': nnef_clamp,
    'matmul': nnef_matmul,
    'conv': nnef_conv,
    'deconv': nnef_deconv,
    'box': nnef_box,
    'debox': nnef_debox,
    'argmax_pool': nnef_argmax_pool,
    # 'sample': unsupported,
    'desample': nnef_desample,
    'nearest_downsample': nnef_nearest_downsample,
    'area_downsample': nnef_area_downsample,
    'nearest_upsample': nnef_nearest_upsample,
    'multilinear_upsample': nnef_multilinear_upsample,
    'sum_reduce': nnef_sum_reduce,
    'max_reduce': nnef_max_reduce,
    'min_reduce': nnef_min_reduce,
    'argmax_reduce': nnef_argmax_reduce,
    'argmin_reduce': nnef_argmin_reduce,
    'mean_reduce': nnef_mean_reduce,
    'moments': nnef_moments,
    'relu': F.relu,
    'sigmoid': torch.sigmoid,
    'tanh': torch.tanh,
    'softabs': lambda x, epsilon: torch.sqrt(torch.pow(x, 2.0) + epsilon),
    'softmax': nnef_softmax,
    'softplus': lambda x: torch.log(torch.exp(x) + 1.0),
    'elu': F.elu,
    'prelu': lambda x, alpha: F.prelu(x, alpha),
    'leaky_relu': lambda x, alpha: F.leaky_relu(x, alpha),
    'max_pool_with_index': nnef_max_pool_with_index,
    'max_pool': nnef_max_pool,
    'avg_pool': nnef_avg_pool,
    'rms_pool': nnef_rms_pool,
    'linear': nnef_linear,
    'separable_conv': nnef_separable_conv,
    'separable_deconv': nnef_separable_deconv,
    'local_response_normalization': nnef_local_response_normalization,
    'local_mean_normalization': nnef_local_mean_normalization,
    'local_variance_normalization': nnef_local_variance_normalization,
    'local_contrast_normalization': nnef_local_contrast_normalization,
    'l1_normalization': nnef_l1_normalization,
    'l2_normalization': nnef_l2_normalization,
    'batch_normalization': nnef_batch_normalization,
    # 'avg_roi_pool': unsupported,
    # 'max_roi_pool': unsupported,
    # 'roi_resample': unsupported,
    # 'avg_roi_align': unsupported,
    # 'max_roi_align': unsupported,
    'linear_quantize': nnef_linear_quantize,
    'logarithmic_quantize': nnef_logarithmic_quantize,
    'copy_n': nnef_copy_n,
    'sin': lambda x: torch.sin(x),
    'cos': lambda x: torch.cos(x),
    'tile': lambda input, repeats: input.repeat(*repeats),
    'pad': nnef_pad,
    'any_reduce': lambda input, axes: _nnef_generic_reduce(input, axes=axes, f=torch.any),
    'all_reduce': lambda input, axes: _nnef_generic_reduce(input, axes=axes, f=torch.all),
}
