from aenum import Enum, extend_enum
from rasterio.enums import Resampling

from gfw_pixetl import get_module_logger

LOGGER = get_module_logger(__name__)


class ResamplingMethodEnum(str, Enum):
    pass


# Dynamically create string enum for RasterIO Resampling methods
for item in Resampling:
    extend_enum(ResamplingMethodEnum, item.name, item.name)


def resampling_factory(method: str) -> Resampling:
    try:
        LOGGER.debug(f"Set resampling method to `{method}`.")
        resampling: Resampling = Resampling[method]
    except KeyError:
        raise ValueError(f"Resampling method `{method}` is not supported.")

    return resampling
