import logging


LOGGER = logging.getLogger(__name__)


def resolve_torch_device(requested: str, torch_module=None) -> str:
    requested = str(requested).strip().lower()
    if requested not in {"auto", "cpu", "cuda"} and not (
        requested.startswith("cuda:") and requested.split(":", 1)[1].isdigit()
    ):
        raise ValueError("device must be auto, cpu, cuda, or cuda:<index>")
    if requested == "cpu":
        return "cpu"
    try:
        torch = torch_module
        if torch is None:
            import torch
        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else 0
    except Exception as exc:
        if requested == "auto":
            LOGGER.warning("requested_device=auto; CUDA initialization failed, using cpu")
            return "cpu"
        raise ValueError("CUDA is unavailable") from exc
    if not available or count <= 0:
        if requested == "auto":
            LOGGER.warning("requested_device=auto; CUDA unavailable, using cpu")
            return "cpu"
        raise ValueError("CUDA is unavailable")
    index = 0 if requested in {"auto", "cuda"} else int(requested.split(":", 1)[1])
    if index >= count:
        raise ValueError(f"CUDA device index {index} is unavailable")
    return f"cuda:{index}"
