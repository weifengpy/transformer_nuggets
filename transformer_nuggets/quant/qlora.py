import logging
import math
from dataclasses import dataclass
from typing import Tuple, Dict, Any, Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
from transformer_nuggets.quant.deqaunt_kernel import dequant_nf4_tensor
from transformer_nuggets.quant.nf4_tensor import NF4Tensor, SubclassTensorArgs

logging.basicConfig(level=logging.INFO)

bnb_available = False


class LinearNF4(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input: torch.Tensor, weight: NF4Tensor):
        ctx.nf4_weight = weight
        return F.linear(input, weight.get_original_weight())

    @staticmethod
    def backward(ctx, grad_output):
        weight: NF4Tensor = ctx.nf4_weight
        return grad_output @ weight.get_original_weight(), None


def linear_nf4(input: torch.Tensor, weight: NF4Tensor) -> torch.Tensor:
    return LinearNF4.apply(input, weight)


class LinearNF4Triton(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input: torch.Tensor, weight: NF4Tensor):
        ctx.nf4_weight = weight
        return F.linear(input, dequant_nf4_tensor(weight))

    @staticmethod
    def backward(ctx, grad_output):
        weight: NF4Tensor = ctx.nf4_weight
        return grad_output @ dequant_nf4_tensor(weight), None


def linear_nf4_trtion(input: torch.Tensor, weight: NF4Tensor) -> torch.Tensor:
    return LinearNF4Triton.apply(input, weight)


def build_input_weight(embed_dim: int, device: torch.device):
    torch.manual_seed(0)
    input_weight = torch.empty(embed_dim, embed_dim, device=device, dtype=torch.bfloat16)
    input_weight.normal_(0, 1)
    return input_weight


def build_bitsandbytes_linear(input_weight: torch.Tensor, device: torch.device):
    global bnb
    if "bnb" not in globals():
        import bitsandbytes as bnb
    param = bnb.nn.Params4bit(input_weight, requires_grad=False, quant_type="nf4").cuda(device)
    bnb_linear = bnb.nn.LinearNF4(input_weight.size(0), input_weight.size(1), bias=False)
    bnb_linear.weight = param
    bnb_linear.to(device)
    return bnb_linear


def get_sample_inputs(
    bsz: int, seqlen: int, embed_dim: int, device: torch.device, requires_grad: bool = False
) -> torch.Tensor:
    sample_input = torch.rand(
        bsz, seqlen, embed_dim, device=device, dtype=torch.bfloat16, requires_grad=requires_grad
    )
    sample_input = sample_input.view(bsz * seqlen, embed_dim)
    return sample_input


def get_mlp_weights(
    embed_dim: int, device: torch.dtype = "cuda"
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """These three weights take up
    3 * (embed_dim * n_hidden) * 2 bytes of memory
    i.g. for embed_dim = 4096 and hidden_dim = 11008
    Total memory usage is 270532608 bytes or 0.27 gb
    """
    torch.manual_seed(0)

    def find_multiple(n: int, k: int) -> int:
        if n % k == 0:
            return n
        return n + k - (n % k)

    hidden_dim = 4 * embed_dim
    n_hidden = int(2 * hidden_dim / 3)
    n_hidden = find_multiple(n_hidden, 256)
    weight1 = torch.empty((n_hidden, embed_dim), dtype=torch.bfloat16, device=device).normal_(0, 1)
    weight2 = torch.empty((n_hidden, embed_dim), dtype=torch.bfloat16, device=device).normal_(0, 1)
    weight3 = torch.empty((embed_dim, n_hidden), dtype=torch.bfloat16, device=device).normal_(0, 1)

    return weight1, weight2, weight3


class MLP(nn.Module):
    def __init__(self, weight1, weight2, weight3) -> None:
        super().__init__()
        self.w1, self.w2, self.w3 = weight1, weight2, weight3

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(F.linear(x, self.w1)) * F.linear(x, self.w2)
        x = F.linear(x, self.w3)
        return x


class NF4MLP(nn.Module):
    def __init__(self, weight1, weight2, weight3) -> None:
        super().__init__()
        self.w1 = NF4Tensor.from_tensor(weight1)
        self.w2 = NF4Tensor.from_tensor(weight2)
        self.w3 = NF4Tensor.from_tensor(weight3)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(linear_nf4(x, self.w1)) * linear_nf4(x, self.w2)
        x = linear_nf4(x, self.w3)
        return x


class NF4MLPTriton(nn.Module):
    def __init__(self, weight1, weight2, weight3) -> None:
        super().__init__()
        self.w1 = NF4Tensor.from_tensor(weight1)
        self.w2 = NF4Tensor.from_tensor(weight2)
        self.w3 = NF4Tensor.from_tensor(weight3)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(linear_nf4_trtion(x, self.w1)) * linear_nf4_trtion(x, self.w2)
        x = linear_nf4_trtion(x, self.w3)
        return x


class BnbQloraMLP(nn.Module):
    def __init__(self, weight1, weight2, weight3, device) -> None:
        super().__init__()
        self.w1 = build_bitsandbytes_linear(weight1, device)
        self.w2 = build_bitsandbytes_linear(weight2, device)
        self.w3 = build_bitsandbytes_linear(weight3, device)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(self.w1(x)) * self.w2(x)
        x = self.w3(x)
        return x


class QloraLinear(nn.Module):
    def __init__(
        self,
        in_features: int,
        out_features: int,
        weight: torch.Tensor,
        r: int,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        register_nf4_param: bool = False,
    ) -> None:
        super().__init__()
        if register_nf4_param:
            self.weight = nn.Parameter(NF4Tensor.from_tensor(weight), requires_grad=False)
        else:
            self.weight = NF4Tensor.from_tensor(weight)
        self.r = r
        self.lora_alpha = lora_alpha
        self.in_features = in_features
        self.out_features = out_features
        self.lora_A = nn.Parameter(weight.new_zeros((r, in_features)))
        self.lora_B = nn.Parameter(weight.new_zeros((out_features, r)))
        self.scaling = self.lora_alpha / self.r

        # Optional dropout
        if lora_dropout > 0.0:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        nn.init.zeros_(self.lora_B)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        result = linear_nf4(x, self.weight)
        result2 = (
            result
            + (self.lora_dropout(x) @ self.lora_A.transpose(0, 1) @ self.lora_B.transpose(0, 1))
            * self.scaling
        )
        return result2

    def fsdp_extensions(self) -> Dict[str, Any]:
        from torch.distributed._composable.fsdp import FSDPTensorExtensions

        weight_extensions = FSDPTensorExtensions(
            self._fsdp_pre_all_gather, self._fsdp_post_all_gather
        )
        return {"weight": weight_extensions}

    def _fsdp_pre_all_gather(self, sharded_param: torch.Tensor):
        return (
            sharded_param.quantized_scalers,
            sharded_param.quantization_factor,
            sharded_param.quantized_data,
        ), (
            SubclassTensorArgs(
                sharded_param.size(),
                sharded_param.stride(),
                sharded_param.storage_offset(),
                sharded_param.dtype,
                sharded_param.device,
                sharded_param.requires_grad,
            ),
            sharded_param.block_size,
            sharded_param.n_blocks,
            sharded_param.scaler_block_size,
            sharded_param.scaler_mean,
            sharded_param.nf4,
        )

    def _fsdp_post_all_gather(
        self,
        all_gather_outputs: Tuple[torch.Tensor, ...],
        metadata: Any,
        param_dtype: torch.dtype,
        *,
        out: Optional[torch.Tensor] = None,
    ) -> Union[Tuple[NF4Tensor, Tuple[torch.Tensor, ...]], None]:
        (quantized_scalers, quantization_factor, quantized_data) = all_gather_outputs
        (tensor_meta, block_size, n_blocks, scaler_block_size, scaler_mean, nf4)  = metadata
        tensor_meta.original_shape = torch.Size([quantized_data.size(0) * 2])
        if out is not None:
            assert isinstance(out, NF4Tensor), f"{type(out)}"
            assert (
                quantized_scalers.untyped_storage().data_ptr()
                == out.quantized_scalers.untyped_storage().data_ptr() and
                quantization_factor.untyped_storage().data_ptr()
                == out.quantization_factor.untyped_storage().data_ptr() and
                quantized_data.untyped_storage().data_ptr()
                == out.quantized_data.untyped_storage().data_ptr()
            ), f"Expects out's data to be the all-gather output"
            return

        return NF4Tensor(
            tensor_meta,
            block_size,
            n_blocks,
            scaler_block_size,
            quantized_scalers,
            quantization_factor,
            scaler_mean,
            quantized_data,
            nf4,
        ), (quantized_scalers, quantization_factor, quantized_data)


@dataclass
class QloraConfig:
    lora_r: int = 2
    lora_alpha: int = 1
    lora_dropout: float = 0.0
    register_nf4_param: bool = False


class QloraMLP(nn.Module):
    # This very notably doesn't save on backward compute
    def __init__(
        self,
        weight1: torch.Tensor,
        weight2: torch.Tensor,
        weight3: torch.Tensor,
        QloraConfig: QloraConfig = None,
    ) -> None:
        super().__init__()
        if QloraConfig is None:
            QloraConfig = QloraConfig()

        lora_r = QloraConfig.lora_r
        lora_alpha = QloraConfig.lora_alpha
        lora_dropout = QloraConfig.lora_dropout
        register_nf4_param = QloraConfig.register_nf4_param

        self.qlora_w1 = QloraLinear(
            weight1.shape[1], weight1.shape[0], weight1, lora_r, lora_alpha, lora_dropout, register_nf4_param
        )
        self.qlora_w2 = QloraLinear(
            weight2.shape[1], weight2.shape[0], weight2, lora_r, lora_alpha, lora_dropout, register_nf4_param
        )
        self.qlora_w3 = QloraLinear(
            weight3.shape[1], weight3.shape[0], weight3, lora_r, lora_alpha, lora_dropout, register_nf4_param
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(self.qlora_w1(x)) * self.qlora_w3(x)
        x = self.qlora_w2(x)
        return x


def swap_for_qlora(model: torch.nn.Module, qlora_config: QloraConfig, dtype) -> None:
    # logging.info("Swapping for Qlora...")
    for module in model.layers:
        feed_forward = module.feed_forward
        w1 = feed_forward.w1.weight.to(dtype=dtype)
        w2 = feed_forward.w2.weight.to(dtype=dtype)
        w3 = feed_forward.w3.weight.to(dtype=dtype)
        new_mod = QloraMLP(w1, w2, w3, qlora_config)
        module.feed_forward = new_mod

    for name, param in model.named_parameters():
        if "lora_" not in name:
            param.requires_grad = False

class QloraLinearDebug(nn.Module):
    def __init__(
        self,
        in_features: int,
        out_features: int,
        weight: torch.Tensor,
        r: int,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
    ) -> None:
        super().__init__()
        self.weight = nn.Parameter(weight.new_zeros((weight.shape[0], int(weight.shape[1]/4))), requires_grad=False)
        # self.weight = weight.new_zeros((weight.shape[0], int(weight.shape[1]/4)))
        # self.weight.requires_grad = False
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        self.r = r
        self.lora_alpha = lora_alpha
        self.in_features = in_features
        self.out_features = out_features
        self.lora_A = nn.Parameter(weight.new_zeros((r, in_features)))
        self.lora_B = nn.Parameter(weight.new_zeros((out_features, r)))
        self.scaling = self.lora_alpha / self.r

        # Optional dropout
        if lora_dropout > 0.0:
            self.lora_dropout = nn.Dropout(p=lora_dropout)
        else:
            self.lora_dropout = lambda x: x

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        nn.init.zeros_(self.lora_B)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        result = F.linear(x, self.weight.repeat(1, 4))
        result2 = (
            result
            + (self.lora_dropout(x) @ self.lora_A.transpose(0, 1) @ self.lora_B.transpose(0, 1))
            * self.scaling
        )
        return result2

class QloraMLPDebug(nn.Module):
    # This very notably doesn't save on backward compute
    def __init__(
        self,
        weight1: torch.Tensor,
        weight2: torch.Tensor,
        weight3: torch.Tensor,
        QloraConfig: QloraConfig = None,
    ) -> None:
        super().__init__()
        if QloraConfig is None:
            QloraConfig = QloraConfig()

        lora_r = QloraConfig.lora_r
        lora_alpha = QloraConfig.lora_alpha
        lora_dropout = QloraConfig.lora_dropout

        self.qlora_w1 = QloraLinearDebug(
            weight1.shape[1], weight1.shape[0], weight1, lora_r, lora_alpha, lora_dropout
        )
        self.qlora_w2 = QloraLinearDebug(
            weight2.shape[1], weight2.shape[0], weight2, lora_r, lora_alpha, lora_dropout
        )
        self.qlora_w3 = QloraLinearDebug(
            weight3.shape[1], weight3.shape[0], weight3, lora_r, lora_alpha, lora_dropout
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = F.silu(self.qlora_w1(x)) * self.qlora_w3(x)
        x = self.qlora_w2(x)
        return x

def swap_for_qlora_debug(model: torch.nn.Module, qlora_config: QloraConfig, dtype) -> None:
    logging.info("Swapping for Qlora...")
    for module in tqdm(model.layers):
        feed_forward = module.feed_forward
        w1 = feed_forward.w1.weight.to(dtype=dtype)
        w2 = feed_forward.w2.weight.to(dtype=dtype)
        w3 = feed_forward.w3.weight.to(dtype=dtype)
        new_mod = QloraMLPDebug(w1, w2, w3, qlora_config)
        module.feed_forward = new_mod

    for name, param in model.named_parameters():
        if "lora_" not in name:
            param.requires_grad = False
