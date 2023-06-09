import torch
import transformer_nuggets.quant as quant
from transformer_nuggets.quant import QLoRAWeight, QLoRAWeightDebug
from torch.testing import assert_close
import pytest


@pytest.mark.parametrize("scaler_block_size", [256])
@pytest.mark.parametrize("block_size", [64, 32])
def test_single_to_double_quantization(block_size: int, scaler_block_size: int):
    torch.manual_seed(0)
    input_weight = torch.empty(1, 16384, device="cuda", dtype=torch.bfloat16)
    input_weight = input_weight.normal_(0, 1)

    qlora = QLoRAWeight(input_weight, block_size)
    single_quantization = quant.get_block_absmax(input_weight.flatten(), block_size)
    double_quantization = qlora.dequantize_scalers(
        qlora.quantized_scalers, qlora.quantization_factor, scaler_block_size
    )

    assert qlora.quantized_scalers.dtype == torch.int8
    assert qlora.scalers.dtype == input_weight.dtype

    assert_close(single_quantization, double_quantization, atol=2e-2, rtol=2e-2)


@pytest.mark.parametrize("block_size, scaler_block_size", [(64, 256)])
def test_reconstruction(block_size: int, scaler_block_size: int):
    torch.manual_seed(0)
    device = "cuda"
    input_weight = torch.empty(1, 16384, device=device, dtype=torch.bfloat16)
    input_weight = input_weight.normal_(0, 1)

    qlora_debug = QLoRAWeightDebug(input_weight, block_size)
    qlora = QLoRAWeight(input_weight, block_size, scaler_block_size)
    max_abs_debug = (qlora_debug.get_original_weight().to(device) - input_weight).abs().max()
    max_abs = (qlora.get_original_weight() - input_weight).abs().max()

    assert abs(max_abs_debug - max_abs) < 1e-2
