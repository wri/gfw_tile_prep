import os
from typing import Set
from unittest import mock

from gfw_pixetl import layers
from gfw_pixetl.grids import LatLngGrid, grid_factory
from gfw_pixetl.models.pydantic import LayerModel
from gfw_pixetl.pipes import RasterPipe
from gfw_pixetl.sources import Destination
from gfw_pixetl.tiles import RasterSrcTile
from tests import minimal_layer_dict

os.environ["ENV"] = "test"

GRID_10 = grid_factory("10/40000")
GRID_1 = grid_factory("1/4000")

LAYER_DICT = {
    **minimal_layer_dict,
    "dataset": "aqueduct_erosion_risk",
    "version": "v201911",
    "pixel_meaning": "level",
    "no_data": 0,
}
LAYER = layers.layer_factory(LayerModel.parse_obj(LAYER_DICT))

SUBSET = ["10N_010E", "20N_010E", "30N_010E"]
PIPE = RasterPipe(LAYER, SUBSET)


def test_create_tiles_subset():

    with mock.patch.object(
        RasterPipe, "get_grid_tiles", return_value=_get_subset_tiles()
    ):
        with mock.patch.object(
            RasterSrcTile, "within", return_value=True
        ), mock.patch.object(
            Destination, "exists", return_value=False
        ), mock.patch.object(
            RasterSrcTile, "transform", return_value=True
        ), mock.patch.object(
            RasterSrcTile, "create_gdal_geotiff", return_value=None
        ), mock.patch.object(
            RasterSrcTile, "upload", return_value=None
        ), mock.patch.object(
            RasterSrcTile, "rm_local_src", return_value=None
        ), mock.patch(
            "gfw_pixetl.utils.upload_geometries.upload_geojsons", return_value=None
        ):
            (
                tiles,
                skipped_tiles,
                failed_tiles,
            ) = PIPE.create_tiles(overwrite=True)
            assert len(tiles) == 1
            assert len(skipped_tiles) == 3
            assert len(failed_tiles) == 0


def test_create_tiles_all():
    pipe = RasterPipe(LAYER)
    with mock.patch.object(
        RasterPipe, "get_grid_tiles", return_value=_get_subset_tiles()
    ), mock.patch.object(RasterSrcTile, "within", return_value=True), mock.patch.object(
        Destination, "exists", return_value=False
    ), mock.patch.object(
        RasterSrcTile, "transform", return_value=True
    ), mock.patch.object(
        RasterSrcTile, "create_gdal_geotiff", return_value=None
    ), mock.patch.object(
        RasterSrcTile, "upload", return_value=None
    ), mock.patch.object(
        RasterSrcTile, "rm_local_src", return_value=None
    ), mock.patch(
        "gfw_pixetl.utils.upload_geometries.upload_geojsons", return_value=None
    ):
        (
            tiles,
            skipped_tiles,
            failed_tiles,
        ) = pipe.create_tiles(overwrite=True)
        assert len(tiles) == 4
        assert len(skipped_tiles) == 0
        assert len(failed_tiles) == 0


def test_filter_src_tiles():
    tiles = _get_subset_tiles()

    with mock.patch.object(RasterSrcTile, "within", return_value=False):
        pipe = tiles | PIPE.filter_src_tiles
        i = 0
        for tile in pipe.results():
            if tile.status == "pending":
                i += 1
                assert isinstance(tile, RasterSrcTile)
        assert i == 0

    with mock.patch.object(RasterSrcTile, "within", return_value=True):
        pipe = tiles | PIPE.filter_src_tiles
        i = 0
        for tile in pipe.results():
            if tile.status == "pending":
                i += 1
                assert isinstance(tile, RasterSrcTile)
        assert i == 4


def test_transform():
    with mock.patch.object(RasterSrcTile, "transform", return_value=True):
        tiles = PIPE.transform(_get_subset_tiles())
        i = 0
        for tile in tiles:
            if tile.status == "pending":
                i += 1
                assert isinstance(tile, RasterSrcTile)
        assert i == 4


def _get_subset_tiles() -> Set[RasterSrcTile]:
    layer_dict = {
        **minimal_layer_dict,
        "dataset": "aqueduct_erosion_risk",
        "version": "v201911",
        "pixel_meaning": "level",
        "no_data": 0,
        "grid": "1/4000",
    }
    layer = layers.layer_factory(LayerModel.parse_obj(layer_dict))

    assert isinstance(layer, layers.RasterSrcLayer)

    pipe = RasterPipe(layer)

    tiles = set()
    for i in range(10, 12):
        for j in range(10, 12):
            assert isinstance(pipe.grid, LatLngGrid)
            tile_id = pipe.grid.xy_to_tile_id(j, i)
            tiles.add(RasterSrcTile(tile_id=tile_id, grid=pipe.grid, layer=layer))

    return tiles
