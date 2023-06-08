import torch
from scipy.stats import norm
import torch
import torch.nn.functional as F
from tqdm import tqdm
import torch.utils.benchmark as benchmark

class QLoRAWeight:
    def __init__(self, inpt_tensor: torch.Tensor, block_size: int = 64):
        assert inpt_tensor.dtype == torch.bfloat16
        assert (
            inpt_tensor.numel() % block_size == 0
        ), "Input tensor must be divisible by block size"
        assert inpt_tensor.dtype == torch.bfloat16, "Input tensor must be bfloat16"
        self.device = inpt_tensor.device
        # Cache the tensor on the class def
        self.nf4 = torch.tensor(
            [
                -1.0000,
                -0.6962,
                -0.5251,
                -0.3949,
                -0.2844,
                -0.1848,
                -0.0911,
                0.0000,
                0.0796,
                0.1609,
                0.2461,
                0.3379,
                0.4407,
                0.5626,
                0.7230,
                1.0000,
            ],
            device=self.device,
            dtype=torch.bfloat16,
        )
        self.block_size = block_size
        self.n_blocks = inpt_tensor.numel() // block_size
        self.scalers = self.get_scalers(inpt_tensor)
        self.norm_float_weight = self.convert_to_norm_float_weight(inpt_tensor.clone())
        self.original_shape = inpt_tensor.shape

    def get_scalers(self, inpt_tensor: torch.Tensor) -> torch.Tensor:
        """Iterate through a flattened tensor getting the scalers for each block"""
        flattened_tensor = inpt_tensor.flatten()
        blocks = flattened_tensor.view(self.n_blocks, self.block_size)
        block_scalers = blocks.abs().max(dim=1).values
        return block_scalers

    def convert_to_norm_float_weight(self, inpt_tensor: torch.Tensor) -> torch.Tensor:
        """Convert a tensor to the normalized float weight format"""
        flattened_tensor = inpt_tensor.flatten()
        #  Since we are using uint8 we will encode 2 entries per byte
        numel = inpt_tensor.numel()
        assert (
            numel % 2 == 0
        ), "Number of elements must be even just to not have to think about the end"
        # Reshape the flattened tensor into blocks of size self.block_size
        blocks = flattened_tensor.view(self.n_blocks, self.block_size)
        # blocks = flattened_tensor.unfold(0, self.block_size, self.block_size)
        # Scale the blocks
        scales = self.scalers.unsqueeze(-1).expand(self.n_blocks, self.block_size)
        scaled_blocks = blocks / scales

        # Returns a flattened tensor with each element quantized to nf4 index
        quantized_blocks = self.quantize_tensor(scaled_blocks.flatten(), self.nf4)

        # Combine the quantized elements into uint8 values
        combined_blocks = quantized_blocks[::2] << 4 | quantized_blocks[1::2]

        return combined_blocks.to(torch.uint8)

    def get_original_weight(self) -> torch.Tensor:
        """Get the original weight from the normalized float weight format"""
        # since we are using uint8 we will decode 2 entries per byte
        # Shift elements down 4 and select out the bottom 4 bits
        first_elements = (self.norm_float_weight >> 4).to(torch.long)
        second_elements = (self.norm_float_weight & 0b1111).to(torch.long)

        # Dequantize every element
        dequantized_first = self.dequantize(first_elements, self.nf4)
        dequantized_second = self.dequantize(second_elements, self.nf4)

        # Build up matrix of scalers repeated for each element in the block
        # Since first and second elements make up a full block, so
        # we expand out to half the size of the full block
        repeated = self.scalers.unsqueeze(-1).expand(self.scalers.size(0), self.block_size // 2)

        scaled_first = dequantized_first * repeated.flatten()
        scaled_second = dequantized_second * repeated.flatten()

        # Flip them to be vertical and them stack them together horizontally
        # Upon flattening this will interleave the elements
        scaled_first = scaled_first.unsqueeze(-1).transpose(0, 1)
        scaled_second = scaled_second.unsqueeze(-1).transpose(0, 1)
        return torch.stack([scaled_first, scaled_second], dim=-1).reshape(self.original_shape)

    @staticmethod
    def quantize_tensor(value: torch.float16, nf4: torch.Tensor) -> torch.Tensor:
        """Quantize a float16 tensor to nf4 format"""
        # Add a new dimension to the value tensor to enable broadcasting
        value = value.unsqueeze(-1)  # (numel, 1)
        # Compare the value tensor with the nf4 tensor element-wise
        mask = value <= nf4
        # Find the index of the first True value along the last dimension
        # Argmax isn't defined on bool tensors, so do the lil trick below
        indexes = 16 - mask.sum(dim=-1)
        # Set the appropriate 4 bits to 1
        # TODO Dont know if i need to the or 0 here
        return 0 | indexes

    @staticmethod
    def dequantize(value: torch.Tensor, nf4: torch.Tensor) -> torch.Tensor:
        """Dequantize a nf4 value to float16 format"""
        # return nf4.index_select(0, value)
        return nf4[value]



class QLoRAWeightDebug:
    """QLoRA Weight written in a more Debug friendly manner"""

    @staticmethod
    def get_nkf(cached=True) -> torch.Tensor:
        if cached:
            return torch.tensor(
                [
                    -1.0000,
                    -0.6962,
                    -0.5251,
                    -0.3949,
                    -0.2844,
                    -0.1848,
                    -0.0911,
                    0.0000,
                    0.0796,
                    0.1609,
                    0.2461,
                    0.3379,
                    0.4407,
                    0.5626,
                    0.7230,
                    1.0000,
                ]
            )

        offset = 0.9677083
        v1 = norm.ppf(torch.linspace(offset, 0.5, 9)[:-1]).tolist()
        # v2 = [0]*(256-15)
        v3 = (-norm.ppf(torch.linspace(offset, 0.5, 8)[:-1])).tolist()
        # v = v1 + v3 + 0.0
        nkf = torch.tensor(v1 + v3 + [0.0])
        nkf = nkf.sort().values
        nkf /= nkf.max()
        return nkf

    @staticmethod
    def quantize(value: torch.float16, nkf: torch.Tensor) -> torch.Tensor:
        """Quantize a float16 value to nkf format"""
        for i in range(len(nkf)):
            if value <= nkf[i]:
                # print("value", value, "nkf", nkf[i])
                return 0 | i
        return 0 | (len(nkf) - 1)

    @staticmethod
    def dequantize(value: torch.Tensor, nkf: torch.Tensor) -> torch.Tensor:
        """Dequantize a nkf value to float16 format"""
        # return nkf.index_select(0, value)
        return nkf[value]

    def __init__(self, inpt_tensor: torch.Tensor, block_size=64):
        assert inpt_tensor.dtype == torch.bfloat16
        assert (
            inpt_tensor.numel() % block_size == 0
        ), "Input tensor must be divisible by block size"
        self.block_size = block_size
        self.n_blocks = inpt_tensor.numel() // block_size
        self.scalers = self.get_scalers(inpt_tensor)
        self.norm_float_weight = self.get_norm_float_weight(inpt_tensor.clone())
        self.original_shape = inpt_tensor.shape

    def get_scalers(self, inpt_tensor: torch.Tensor):
        """Iterate through a flattened tensor getting the scalers for each block"""
        flattened_tensor = inpt_tensor.flatten()
        block_scalers = []
        for block_start in range(0, inpt_tensor.numel(), self.block_size):
            block_end = min(block_start + self.block_size, inpt_tensor.numel())
            block = flattened_tensor[block_start:block_end]
            block_max = block.abs().max()
            block_scalers.append(block_max)
        return torch.tensor(block_scalers)

    def get_norm_float_weight(self, inpt_tensor: torch.Tensor):
        nkf = self.get_nkf()
        flattened_tensor = inpt_tensor.flatten()
        #  Since we are using uint8 we will encode 2 entries per byte
        numel = inpt_tensor.numel()
        assert (
            numel % 2 == 0
        ), "Number of elements must be even just to not have to think about the end"
        quantized_length = numel // 2
        quantized_tensor = torch.zeros(quantized_length, dtype=torch.uint8)
        for i in tqdm(range(len(self.scalers))):
            block_start = i * self.block_size
            block_end = min(block_start + self.block_size, flattened_tensor.numel())
            block = flattened_tensor[block_start:block_end]
            # Scale the block
            block /= self.scalers[i]
            # We will iterate over each element in the block and quantize it
            # In groups of 2
            for j in range(0, self.block_size, 2):
                # Combine two bfloat16s via quantization to 4 bit types into a single uint8
                element_1 = self.quantize(block[j], nkf)
                element_2 = self.quantize(block[j + 1], nkf)
                combined = element_1 << 4 | element_2
                quantized_tensor[(i * self.block_size // 2) + j // 2] = combined
        return quantized_tensor

    def get_original_weight(self):
        # since we are using uint8 we will decode 2 entries per byte
        nkf = self.get_nkf()
        original_weight = torch.empty(2 * (self.norm_float_weight.numel()), dtype=torch.bfloat16)
        # Scalers is a proxy for num_blocks
        for i in range(len(self.scalers)):
            block_start = i * self.block_size
            block_end = block_start + self.block_size
            block = original_weight[block_start:block_end]
            for j in range(0, self.block_size, 2):
                combined = self.norm_float_weight[(i * self.block_size // 2) + j // 2]
                # Shift element down 4
                element_1 = combined >> 4
                # Select out the bottom 4 bits
                element_2 = combined & 0b1111
                block[j] = self.dequantize(element_1.item(), nkf) * self.scalers[i]
                block[j + 1] = self.dequantize(element_2.item(), nkf) * self.scalers[i]
        return original_weight.reshape(self.original_shape)