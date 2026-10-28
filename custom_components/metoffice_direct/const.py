"""Provider identifiers, native fields and bounded API policy."""

from datetime import timedelta

DOMAIN = "metoffice_direct"
ATTRIBUTION = "Powered by Met Office data"
HOST = "https://data.hub.api.metoffice.gov.uk"
ROOTS = {
    "global_spot": f"{HOST}/sitespecific/v0",
    "bpf": f"{HOST}/mo-blended-prob-forecast-feature-svc/2.0.0",
    "warnings": f"{HOST}/nswws/v1.1",
    "observations": f"{HOST}/observation-land/1",
    "map_images": f"{HOST}/map-images/1.0.0",
    "atmospheric": f"{HOST}/atmospheric-models/1.0.0",
}
# Deliberately below the subscribed ceilings; requests include discovery and retries.
DAILY_LIMITS = {
    "global_spot": 330,
    "bpf": 50,
    "warnings": 19000,
    "observations": 330,
    "map_images": 900,
    "atmospheric": 0,  # No atmospheric requests until a bounded byte-budget design exists.
}
INTERVALS = {
    "hourly": timedelta(minutes=20),
    "daily": timedelta(hours=1),
    "three_hourly": timedelta(hours=1),
    "warnings": timedelta(minutes=1),
    "observations": timedelta(minutes=15),
    "bpf": timedelta(hours=2),
    "map_images": timedelta(hours=1),
}
MAX_AGE = {
    "hourly": timedelta(hours=3),
    "daily": timedelta(hours=6),
    "three_hourly": timedelta(hours=4),
    "warnings": timedelta(minutes=10),
    "observations": timedelta(hours=3),
    "bpf": timedelta(hours=6),
    "map_images": timedelta(hours=3),
}
CONDITIONS = {
    0: "clear-night",
    1: "sunny",
    2: "partlycloudy",
    3: "partlycloudy",
    5: "fog",
    6: "fog",
    7: "cloudy",
    8: "cloudy",
    **dict.fromkeys(range(9, 13), "rainy"),
    **dict.fromkeys(range(13, 16), "pouring"),
    **dict.fromkeys(range(16, 19), "snowy-rainy"),
    **dict.fromkeys(range(19, 22), "hail"),
    **dict.fromkeys(range(22, 28), "snowy"),
    28: "lightning-rainy",
    29: "lightning-rainy",
    30: "lightning",
}
# key: (friendly name, unit, SensorDeviceClass value or None)
POINT_FIELDS = {
    "screenTemperature": ("Temperature", "°C", "temperature"),
    "screenDewPointTemperature": ("Dew point", "°C", "temperature"),
    "feelsLikeTemperature": ("Feels like temperature", "°C", "temperature"),
    "maxScreenAirTemp": ("Previous hour maximum temperature", "°C", "temperature"),
    "minScreenAirTemp": ("Previous hour minimum temperature", "°C", "temperature"),
    "screenRelativeHumidity": ("Humidity", "%", "humidity"),
    "windSpeed10m": ("Wind speed", "m/s", "wind_speed"),
    "windGustSpeed10m": ("Wind gust", "m/s", "wind_speed"),
    "max10mWindGust": ("Previous hour maximum gust", "m/s", "wind_speed"),
    "windDirectionFrom10m": ("Wind direction", "°", "wind_direction"),
    "mslp": ("Pressure", "Pa", "atmospheric_pressure"),
    "visibility": ("Visibility", "m", "distance"),
    "uvIndex": ("UV index", "UV index", None),
    "precipitationRate": ("Precipitation rate", "mm/h", "precipitation_intensity"),
    "totalPrecipAmount": ("Previous hour precipitation", "mm", "precipitation"),
    "totalSnowAmount": ("Previous hour snow water equivalent", "mm", "precipitation"),
    "probOfPrecipitation": ("Precipitation probability", "%", None),
}
OBSERVATION_FIELDS = {
    "temperature": ("Temperature", "°C", "temperature"),
    "humidity": ("Humidity", "%", "humidity"),
    "mslp": ("Pressure", "hPa", "atmospheric_pressure"),
    "visibility": ("Visibility", "m", "distance"),
    "wind_speed": ("Wind speed", "m/s", "wind_speed"),
    "wind_gust": ("Wind gust", "m/s", "wind_speed"),
    "derived_dew_point": ("Estimated dew point", "°C", "temperature"),
}
BPF_FIELDS = {
    "airTemperature1p5m": ("Temperature median", "°C", "temperature"),
    "relativeHumidity1p5m": ("Humidity median", "%", "humidity"),
    "cloudAreaFraction": ("Cloud cover median", "%", None),
    "lowTypeCloudAreaFraction": ("Low cloud cover median", "%", None),
    "airPressureAtSeaLevel": ("Pressure median", "Pa", "atmospheric_pressure"),
    "feelsLikeTemperature1p5m": ("Feels like median", "°C", "temperature"),
    "windSpeed10m": ("Wind speed median", "m/s", "wind_speed"),
    "windFromDirection10mMean": ("Wind direction", "°", "wind_direction"),
    "windSpeedOfGust10mMaximumPt01h": ("Hourly gust median", "m/s", "wind_speed"),
    "visibilityInAir1p5m": ("Visibility median", "m", "distance"),
    "ultravioletIndex": ("UV index median", "UV index", None),
}
