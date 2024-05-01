"""
Utils for caching the outputs of AOTAutograd
"""
from __future__ import annotations

import dataclasses

import functools
import logging
import os
import pickle
import tempfile
from copy import copy
from typing import Any, cast, Dict, Optional, Str

import torch
from torch._guards import detect_fake_mode
from torch._inductor.codecache import (
    _ident,
    FxGraphCachePickler,
    get_code_hash,
    get_inductor_root,
    write_atomic,
)
from torch._subclasses.fake_tensor import (
    extract_tensor_metadata,
    FakeTensor,
    TensorMetadata,
)
from torch.fx.node import Node

from .schemas import AOTConfig  # noqa: F401

log = logging.getLogger(__name__)


def fake_tensor_from_meta(tensor_meta):
    fake_mode = detect_fake_mode()
    with fake_mode:
        return cast(
            FakeTensor,
            torch.empty_strided(
                tensor_meta.shape,
                tensor_meta.stride,
                device="meta",
                dtype=tensor_meta.dtype,
                requires_grad=tensor_meta.requires_grad,
            ),
        )


class BypassAOTAutogradCache(Exception):
    pass


cache = tempfile.mkdtemp()


def cache_dir():
    """Returns the directory where we store AOTAutograd cache entries."""
    return cache


def check_node_safe(node: Node):
    """
    Checks that the node only uses supported operators.
    """

    def is_torch_function(target):
        if isinstance(target, str):
            name = target
        elif callable(target):
            name = target.__name__
        else:
            return False
        # TODO: is this the right check?
        return name.startswith("torch.")

    def is_tensor(target):
        # Tensors always have example values in meta field
        return "example_value" in target.meta

    # I'd love to use a match statement here, but it wasn't introduced until py3.10
    if node.op == "call_function":
        # We support only torch.* functions for now
        # We can probably add an allowlist of safe non-torch implementations as well
        if not is_torch_function(node.target):
            raise BypassAOTAutogradCache(
                f"Unsupported call_function target {node.target}"
            )
    elif node.op == "call_method":
        method_target = node.args[0]
        if not is_tensor(method_target):
            # We support only method calls on tensors and symints
            raise BypassAOTAutogradCache(
                f"Unsupported call_method target {method_target}"
            )
    # Cache safe
    elif node.op in ("placeholder", "call_module", "get_attr", "output"):
        # TODO: not all call_modules may be safe
        pass
    else:
        raise BypassAOTAutogradCache(f"Unsupported node op {node.op}")


@functools.lru_cache(None)
def get_autograd_code_hash():
    autograd_root = os.path.dirname(__file__)
    inductor_root = get_inductor_root()
    return get_code_hash([autograd_root, inductor_root])


def check_cacheable(gm: torch.fx.GraphModule):
    """
    Checks that the graph module only uses supported operators
    """
    nodes = gm.graph.nodes
    for node in nodes:
        check_node_safe(node)


class AOTAutogradCacheDetails:
    """
    Object to capture all the details for a dynamo graph module relevant to computing
    a safe and stable cache key for AOTAutograd.
    """

    def __init__(self, gm: torch.fx.GraphModule, config: AOTConfig):
        self.gm = gm  # TODO: we'll handle different parts of the graph module
        # TODO: We'll want to handle the full_args passed in as well
        self.config = config  # Gets reduced by the Pickler
        check_cacheable(gm)
        self.code_hash = get_autograd_code_hash()

    def debug_str(self) -> str:
        return AOTAutogradCachePickler.debug_str(self)


def _reduce_aot_config(config: AOTConfig):
    """
    Reduce the config to a stable key for caching.
    """
    return (
        _ident,
        (
            config.num_params_buffers,
            config.keep_inference_input_mutations,
            config.is_export,
            config.no_tangents,
            config.dynamic_shapes,
            config.aot_autograd_arg_pos_to_source,
            config.enable_log,
            config.pre_dispatch,
        ),
    )


class AOTAutogradCachePickler(FxGraphCachePickler):
    dispatch_table = FxGraphCachePickler.dispatch_table.copy()
    dispatch_table[AOTConfig] = _reduce_aot_config


def autograd_cache_hash(
    gm: torch.fx.GraphModule,
    config: AOTConfig,
    # TODO: add args and parameters
) -> str:
    """
    Generate a unique hash of the FX graph for caching.
    """
    details = AOTAutogradCacheDetails(gm, config)
    # The prefix distinguishes among the other kinds of objects we cache
    key = "a" + AOTAutogradCachePickler.get_hash(details)
    log.debug("FX graph cache hash details for key %s:\n%s", key, details.debug_str())
    return key


@dataclasses.dataclass
class SerializedAOTGraphModule:
    module: torch.fx.GraphModule
    node_metas: Dict[Str, Any]


@dataclasses.dataclass
class AOTAutogradCacheEntry:
    """
    A cache entry for a compiled FX graph.
    """

    fw_module: SerializedAOTGraphModule
    bw_module: Optional[SerializedAOTGraphModule] = None


class NodeMetaSerializer:
    """
    Convert a node's meta field into a pickleable dictionary
    """

    SERIALIZABLE_FIELDS = ["tensor_meta", "stack_trace", "seq_nr"]

    @staticmethod
    def serialize(node_meta):
        new_meta = {}
        for key in node_meta:
            try:
                if key in NodeMetaSerializer.SERIALIZABLE_FIELDS:
                    new_meta[key] = node_meta[key]
                else:
                    new_meta[key] = getattr(NodeMetaSerializer, f"serialize_{key}")(
                        node_meta[key]
                    )
            except AttributeError as e:
                raise BypassAOTAutogradCache(
                    f"No serialization implemented for meta field {key} with value {node_meta[key]}."
                    "Implement serialize_{key} and deserialize_{key}"
                ) from e
            except Exception as e:
                raise BypassAOTAutogradCache(
                    f"Failed serializing meta field {key}"
                ) from e
        return new_meta

    @staticmethod
    def serialize_val(val):
        # TODO: handle other possible values
        if not isinstance(val, torch.Tensor):
            return val
        return extract_tensor_metadata(val)

    """ TODO: implement serialization for these fields """

    @staticmethod
    def serialize_source_fn_stack(val):
        return None

    @staticmethod
    def deserialize_source_fn_stack(val):
        return None

    @staticmethod
    def serialize_original_aten(val):
        return None

    @staticmethod
    def deserialize_original_aten(val):
        return None

    @staticmethod
    def serialize_from_node(val):
        return None

    @staticmethod
    def deserialize_from_node(val):
        return None

    @staticmethod
    def deserialize_val(val):
        # TODO: handle other possible values
        if not isinstance(val, TensorMetadata):
            return val
        return fake_tensor_from_meta(val)

    @staticmethod
    def deserialize(serialized_meta):
        new_meta = {}
        for key in serialized_meta:
            try:
                if key in NodeMetaSerializer.SERIALIZABLE_FIELDS:
                    new_meta[key] = serialized_meta[key]
                else:
                    new_meta[key] = getattr(NodeMetaSerializer, f"deserialize_{key}")(
                        serialized_meta[key]
                    )
            except AttributeError as e:
                raise BypassAOTAutogradCache(
                    f"No deserialization implemented for meta field {key}. Implement serialize_{key} and deserialize_{key}"
                ) from e
            except Exception as e:
                raise BypassAOTAutogradCache(
                    f"Failed deserializing meta field {key}"
                ) from e
        return new_meta


def serialize_graph_module(module: torch.fx.GraphModule) -> SerializedAOTGraphModule:
    module = copy(module)
    module.recompile()
    node_metas = {}
    for node in module.graph.nodes:
        node_metas[str(node.target)] = NodeMetaSerializer.serialize(node.meta)
    assert len(module.graph.nodes) == len(node_metas)
    return SerializedAOTGraphModule(module, node_metas)


def deserialize_graph_module(module: SerializedAOTGraphModule) -> torch.fx.GraphModule:
    new_module = module.module
    node_metas = module.node_metas
    for node in new_module.graph.nodes:
        node.meta = NodeMetaSerializer.deserialize(node_metas[str(node.target)])
    new_module.recompile()
    return new_module


class AOTAutogradCache:
    """
    Supports caching and reusing FX graphs created by AOTAutograd.
    Cache entries are stored at
        <temp_dir>/<hash-key>/
    """

    @staticmethod
    def _get_tmp_dir() -> str:
        """
        Get the toplevel temporary directory for storing compiled graphs.
        """
        return os.path.join(cache_dir(), "aotautograd")

    @staticmethod
    def _lookup(key: str):
        """Given a key generated by AOTAutogradCachePickler, look up its location in the cache."""
        subdir = os.path.join(AOTAutogradCache._get_tmp_dir(), key)
        if not os.path.exists(subdir):
            return None
        path = os.path.join(subdir, "entry")
        try:
            entry: AOTAutogradCacheEntry = pickle.load(open(path, "rb"))
            fw_module = deserialize_graph_module(entry.fw_module)
            bw_module = (
                deserialize_graph_module(entry.bw_module)
                if entry.bw_module is not None
                else None
            )
            return (fw_module, bw_module)
        except Exception as e:
            print("AOTAutograd cache unable to load compiled graph:", e)
            raise e

    @staticmethod
    def _save(key: str, fw_module, bw_module=None):
        """Save forward and backward modules to the cache."""
        serialized_fw = serialize_graph_module(fw_module)
        serialized_bw = (
            serialize_graph_module(bw_module) if bw_module is not None else None
        )
        entry = AOTAutogradCacheEntry(serialized_fw, serialized_bw)
        try:
            content = pickle.dumps(entry)
        except Exception as e:
            print("AOTAutograd cache unable to serialize compiled graph: %s", e)
            raise e
        subdir = os.path.join(AOTAutogradCache._get_tmp_dir(), key)
        if not os.path.exists(subdir):
            os.makedirs(subdir, exist_ok=True)
        path = os.path.join(subdir, "entry")
        log.warning("Writing AOTAutograd cache entry to %s", path)
        write_atomic(path, content)
