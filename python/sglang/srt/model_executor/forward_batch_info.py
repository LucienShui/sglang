from dataclasses import dataclass
from enum import IntEnum, auto
from typing import TYPE_CHECKING, List

import numpy as np
import torch

from sglang.srt.managers.schedule_batch import ScheduleBatch
from sglang.srt.mem_cache.memory_pool import BaseTokenToKVPool, ReqToTokenPool

if TYPE_CHECKING:
    from sglang.srt.model_executor.model_runner import ModelRunner


class ForwardMode(IntEnum):
    # Prefill a new sequence. This is deprecated now. "EXTEND" covers this case.
    PREFILL = auto()
    # Extend a sequence. The KV cache of the first part of the sequence is already computed (e.g., system prompt).
    EXTEND = auto()
    # Decode one token.
    DECODE = auto()


@dataclass
class InputMetadata:
    """Store all inforamtion of a forward pass."""

    forward_mode: ForwardMode
    batch_size: int
    req_pool_indices: torch.Tensor
    seq_lens: torch.Tensor
    req_to_token_pool: ReqToTokenPool
    token_to_kv_pool: BaseTokenToKVPool
    out_cache_loc: torch.Tensor

    # Input positions
    positions: torch.Tensor = None

    # For extend
    extend_seq_lens: torch.Tensor = None
    extend_start_loc: torch.Tensor = None
    extend_no_prefix: bool = None

    # For multimodal
    pixel_values: List[torch.Tensor] = None
    image_sizes: List[List[int]] = None
    image_offsets: List[int] = None

    # Output options
    return_logprob: bool = False
    top_logprobs_nums: List[int] = None

    # For multimodal
    pixel_values: List[torch.Tensor] = None
    image_sizes: List[List[int]] = None
    image_offsets: List[int] = None

    # Trition attention backend
    triton_max_seq_len: int = 0
    triton_max_extend_len: int = 0
    triton_start_loc: torch.Tensor = None
    triton_prefix_lens: torch.Tensor = None

    # FlashInfer attention backend
    flashinfer_prefill_wrapper_ragged: "BatchPrefillWithRaggedKVCacheWrapper" = None
    flashinfer_prefill_wrapper_paged: "BatchPrefillWithPagedKVCacheWrapper" = None
    flashinfer_decode_wrapper: "BatchDecodeWithPagedKVCacheWrapper" = None
    flashinfer_use_ragged: bool = False

    def init_multimodal_infos(self, batch: ScheduleBatch):
        reqs = batch.reqs
        self.pixel_values = [r.pixel_values for r in reqs]
        self.image_sizes = [r.image_size for r in reqs]
        self.image_offsets = [
            (r.image_offset - p_len) if r.image_offset is not None else 0
            for r, p_len in zip(reqs, batch.prefix_lens_cpu)
        ]

    def compute_positions(self, batch: ScheduleBatch):
        bs = self.batch_size
        if self.forward_mode == ForwardMode.DECODE:
            self.positions = ((batch.seq_lens - 1) + batch.position_ids_offsets).to(
                torch.int64
            )
        else:
            seq_lens_cpu = batch.seq_lens.cpu().numpy()
            prefix_lens_cpu = batch.prefix_lens_cpu
            position_ids_offsets_cpu = batch.position_ids_offsets.cpu().numpy()
            self.positions = torch.tensor(
                np.concatenate(
                    [
                        np.arange(
                            prefix_lens_cpu[i] + position_ids_offsets_cpu[i],
                            seq_lens_cpu[i] + position_ids_offsets_cpu[i],
                        )
                        for i in range(bs)
                    ],
                    axis=0,
                ),
                device="cuda",
            )

    def compute_extend_infos(self, batch: ScheduleBatch):
        self.extend_seq_lens = torch.tensor(
            batch.extend_lens_cpu, device="cuda", dtype=torch.int32
        )
        self.extend_start_loc = torch.zeros_like(batch.seq_lens)
        self.extend_start_loc[1:] = torch.cumsum(self.extend_seq_lens[:-1], dim=0)
        self.extend_no_prefix = all(l == 0 for l in batch.prefix_lens_cpu)

    @classmethod
    def from_batch(cls, model_runner, batch: ScheduleBatch, forward_mode: ForwardMode):
        ret = cls(
            forward_mode=forward_mode,
            batch_size=batch.batch_size(),
            total_num_tokens=batch.total_num_tokens,
            req_pool_indices=batch.req_pool_indices,
            seq_lens=batch.seq_lens,
            req_to_token_pool=model_runner.req_to_token_pool,
            token_to_kv_pool=model_runner.token_to_kv_pool,
            out_cache_loc=batch.out_cache_loc,
            return_logprob=batch.return_logprob,
            top_logprobs_nums=batch.top_logprobs_nums,
        )

        ret.compute_positions(batch)

        if forward_mode != ForwardMode.DECODE:
            ret.init_multimodal_infos(batch)
            ret.compute_extend_infos(batch)

        prefix_lens = (
            torch.tensor(batch.prefix_lens_cpu, device="cuda", dtype=torch.int32)
            if forward_mode != ForwardMode.DECODE
            else None
        )

        if model_runner.server_args.disable_flashinfer:
            ret.init_triton_args(prefix_lens)
        else:
            flashinfer_use_ragged = False
            if (
                forward_mode != ForwardMode.DECODE
                and int(torch.sum(batch.seq_lens)) > 4096
            ):
                flashinfer_use_ragged = True
            ret.init_flashinfer_args(model_runner, prefix_lens, flashinfer_use_ragged)

        return ret

    @classmethod
    def create(
        cls,
        model_runner,
        forward_mode,
        req_pool_indices,
        seq_lens,
        prefix_lens,
        position_ids_offsets,
        out_cache_loc,
        top_logprobs_nums=None,
        return_logprob=False,
        skip_flashinfer_init=False,
    ):
        batch_size = len(req_pool_indices)

        if self.forward_mode == ForwardMode.DECODE:
            if True:
                self.positions = self.seq_lens - 1
            else:
                # Deprecated
                self.positions = (self.seq_lens - 1) + position_ids_offsets
        else:
            if True:
                self.positions = torch.tensor(
                    np.concatenate(
                        [
                            np.arange(len(req.prefix_indices), len(req.input_ids))
                            for req in batch.reqs
                        ],
                        axis=0,
                    ),
                    device="cuda",
                )
            else:
                # Deprecated
                position_ids_offsets_cpu = position_ids_offsets.cpu().numpy()
                self.positions = torch.tensor(
                    np.concatenate(
                        [
                            np.arange(
                                len(req.prefix_indices) + position_ids_offsets_cpu[i],
                                len(req.input_ids) + position_ids_offsets_cpu[i],
                            )
                            for i, req in enumerate(batch.reqs)
                        ],
                        axis=0,
                    ),
                    device="cuda",
                )

        # Positions should be in long type
        self.positions = self.positions.to(torch.int64)

    def compute_extend_infos(self, batch: ScheduleBatch):
        if self.forward_mode == ForwardMode.DECODE:
            self.extend_seq_lens = self.extend_start_loc = self.extend_no_prefix = None
        else:
            prefix_lens_cpu = [
                len(r.input_ids) - len(r.prefix_indices) for r in batch.reqs
            ]
            self.extend_seq_lens = torch.tensor(prefix_lens_cpu, device="cuda")
            self.extend_start_loc = torch.zeros_like(self.seq_lens)
            self.extend_start_loc[1:] = torch.cumsum(self.extend_seq_lens[:-1], dim=0)
            self.extend_no_prefix = all(x == 0 for x in prefix_lens_cpu)

    def init_total_num_tokens(self, batch: ScheduleBatch):
        self.total_num_tokens = sum(len(req.input_ids) for req in batch.reqs)

    @classmethod
    def from_schedule_batch(
        cls,
        model_runner: "ModelRunner",
        batch: ScheduleBatch,
        forward_mode: ForwardMode,
    ):
        ret = cls(
            forward_mode=forward_mode,
            batch_size=batch.batch_size(),
            req_pool_indices=batch.req_pool_indices,
            seq_lens=batch.seq_lens,
            req_to_token_pool=model_runner.req_to_token_pool,
            token_to_kv_pool=model_runner.token_to_kv_pool,
            out_cache_loc=out_cache_loc,
            extend_seq_lens=extend_seq_lens,
            extend_start_loc=extend_start_loc,
            extend_no_prefix=extend_no_prefix,
            return_logprob=return_logprob,
            top_logprobs_nums=top_logprobs_nums,
        )

        ret.compute_positions(batch)

        ret.compute_extend_infos(batch)

        ret.init_total_num_tokens(batch)

        if forward_mode != ForwardMode.DECODE:
            ret.init_multimuldal_info(batch)

        prefix_lens = None
        if forward_mode != ForwardMode.DECODE:
            prefix_lens = torch.tensor(
                [len(r.prefix_indices) for r in batch.reqs], device="cuda"
            )

        if model_runner.server_args.disable_flashinfer:
            ret.init_triton_args(prefix_lens)

        if not skip_flashinfer_init and not model_runner.server_args.disable_flashinfer:
            flashinfer_use_ragged = False
            if forward_mode != ForwardMode.DECODE and int(torch.sum(seq_lens)) > 4096:
                flashinfer_use_ragged = True
            ret.init_flashinfer_args(model_runner, prefix_lens, flashinfer_use_ragged)

        return ret

    def init_triton_args(self, prefix_lens):
        """Init auxiliary variables for triton attention backend."""
        batch_size = len(self.seq_lens)
        self.triton_max_seq_len = int(torch.max(self.seq_lens))
        self.triton_prefix_lens = prefix_lens
        self.triton_start_loc = torch.zeros(
            (batch_size,), dtype=torch.int32, device="cuda"
        )
        self.triton_start_loc[1:] = torch.cumsum(self.seq_lens[:-1], dim=0)

        if self.forward_mode == ForwardMode.DECODE:
            self.triton_max_extend_len = None
        else:
            extend_seq_lens = self.seq_lens - prefix_lens
            self.triton_max_extend_len = int(torch.max(extend_seq_lens))

    def init_flashinfer_args(self, model_runner, prefix_lens, flashinfer_use_ragged):
        update_flashinfer_indices(
            self.forward_mode,
            model_runner,
            self.req_pool_indices,
            self.seq_lens,
            prefix_lens,
            model_runner.flashinfer_decode_wrapper,
            flashinfer_use_ragged,
        )

        (
            self.flashinfer_decode_wrapper,
            self.flashinfer_prefill_wrapper_ragged,
            self.flashinfer_prefill_wrapper_paged,
            self.flashinfer_use_ragged,
        ) = (
            model_runner.flashinfer_decode_wrapper,
            model_runner.flashinfer_prefill_wrapper_ragged,
            model_runner.flashinfer_prefill_wrapper_paged,
            flashinfer_use_ragged,
        )


def update_flashinfer_indices(
    forward_mode,
    model_runner,
    req_pool_indices,
    seq_lens,
    prefix_lens,
    flashinfer_decode_wrapper=None,
    flashinfer_use_ragged=False,
):
    """Init auxiliary variables for FlashInfer attention backend."""
    num_qo_heads = model_runner.model_config.num_attention_heads // model_runner.tp_size
    num_kv_heads = model_runner.model_config.get_num_kv_heads(model_runner.tp_size)
    head_dim = model_runner.model_config.head_dim
    batch_size = len(req_pool_indices)

    if flashinfer_use_ragged:
        paged_kernel_lens = prefix_lens
    else:
        paged_kernel_lens = seq_lens

    kv_indptr = torch.zeros((batch_size + 1,), dtype=torch.int32, device="cuda")
    kv_indptr[1:] = torch.cumsum(paged_kernel_lens, dim=0)
    req_pool_indices_cpu = req_pool_indices.cpu().numpy()
    paged_kernel_lens_cpu = paged_kernel_lens.cpu().numpy()
    kv_indices = torch.cat(
        [
            model_runner.req_to_token_pool.req_to_token[
                req_pool_indices_cpu[i], : paged_kernel_lens_cpu[i]
            ]
            for i in range(batch_size)
        ],
        dim=0,
    ).contiguous()
    kv_last_page_len = torch.ones((batch_size,), dtype=torch.int32, device="cuda")

    if forward_mode == ForwardMode.DECODE:
        # CUDA graph uses different flashinfer_decode_wrapper
        if flashinfer_decode_wrapper is None:
            flashinfer_decode_wrapper = model_runner.flashinfer_decode_wrapper

        flashinfer_decode_wrapper.end_forward()
        flashinfer_decode_wrapper.begin_forward(
            kv_indptr,
            kv_indices,
            kv_last_page_len,
            num_qo_heads,
            num_kv_heads,
            head_dim,
            1,
        )
    else:
        # extend part
        qo_indptr = torch.zeros((batch_size + 1,), dtype=torch.int32, device="cuda")
        qo_indptr[1:] = torch.cumsum(seq_lens - prefix_lens, dim=0)

        if flashinfer_use_ragged:
            model_runner.flashinfer_prefill_wrapper_ragged.end_forward()
            model_runner.flashinfer_prefill_wrapper_ragged.begin_forward(
                qo_indptr,
                qo_indptr,
                num_qo_heads,
                num_kv_heads,
                head_dim,
            )

        # cached part
        model_runner.flashinfer_prefill_wrapper_paged.end_forward()
        model_runner.flashinfer_prefill_wrapper_paged.begin_forward(
            qo_indptr,
            kv_indptr,
            kv_indices,
            kv_last_page_len,
            num_qo_heads,
            num_kv_heads,
            head_dim,
            1,
        )
