import pytest

from mc_autobuilder.config import Config, ConfigError

VALID_YAML = """
mission_chief:
  game_world: "US"
  base_url: "https://www.missionchief.com"

regions:
  - name: "Bay Area"
    bbox: {north: 38.0, south: 37.2, east: -121.7, west: -122.6}
  - name: "Sacramento"
    city: "Sacramento, CA"
    radius_km: 15

building_types:
  poi_fire_station:
    building_type: 0
    max_per_run: 5
  poi_police:
    building_type: 5
    max_per_run: 5

dedupe:
  radius_m: 150

naming:
  template: "{city} - {poi_name}"

budget:
  max_credits_per_run: 5000000
  credit_reserve: 1000000

rlm_cache:
  ttl_hours: 24
  cache_dir: ".rlm_cache"

rate_limiting:
  min_delay_seconds: 2
  max_delay_seconds: 5
"""


def test_loads_valid_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_YAML)

    config = Config.from_yaml(path)

    assert config.game_world == "US"
    assert len(config.regions) == 2
    assert config.regions[0].name == "Bay Area"
    assert config.regions[0].bbox == {"north": 38.0, "south": 37.2, "east": -121.7, "west": -122.6}
    assert config.regions[1].city == "Sacramento, CA"
    assert config.regions[1].radius_km == 15
    assert len(config.building_types) == 2
    assert config.dedupe_radius_m == 150
    assert config.naming_template == "{city} - {poi_name}"
    assert config.max_credits_per_run == 5_000_000
    assert config.credit_reserve == 1_000_000


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.from_yaml(tmp_path / "does_not_exist.yaml")


def test_missing_game_world_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("regions: []\nbuilding_types: {}\n")
    with pytest.raises(ConfigError, match="game_world"):
        Config.from_yaml(path)


def test_region_with_no_location_mode_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Nowhere"
building_types:
  poi_fire_station: {building_type: 0}
"""
    )
    with pytest.raises(ConfigError, match="exactly one of"):
        Config.from_yaml(path)


def test_region_with_two_location_modes_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Ambiguous"
    bbox: {north: 1, south: 0, east: 1, west: 0}
    city: "Somewhere"
building_types:
  poi_fire_station: {building_type: 0}
"""
    )
    with pytest.raises(ConfigError, match="exactly one of"):
        Config.from_yaml(path)


def test_city_region_without_radius_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Missing Radius"
    city: "Somewhere"
building_types:
  poi_fire_station: {building_type: 0}
"""
    )
    with pytest.raises(ConfigError, match="radius_km"):
        Config.from_yaml(path)


def test_empty_building_types_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Bay Area"
    bbox: {north: 1, south: 0, east: 1, west: 0}
building_types: {}
"""
    )
    with pytest.raises(ConfigError, match="building_types"):
        Config.from_yaml(path)


def test_bad_naming_template_placeholder_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mission_chief:
  game_world: "US"
regions:
  - name: "Bay Area"
    bbox: {north: 1, south: 0, east: 1, west: 0}
building_types:
  poi_fire_station: {building_type: 0}
naming:
  template: "{unsupported_field}"
"""
    )
    with pytest.raises(ConfigError, match="unsupported placeholder"):
        Config.from_yaml(path)
