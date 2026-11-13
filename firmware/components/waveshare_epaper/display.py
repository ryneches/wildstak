# Modified for WildStak from parkghost/esphome-epaper (c6ac50c, Apache-2.0); see README.md.
from esphome import core, pins
import esphome.codegen as cg
from esphome.components import display, spi
import esphome.config_validation as cv
import logging
from esphome.const import (
    CONF_BUSY_PIN,
    CONF_DC_PIN,
    CONF_FULL_UPDATE_EVERY,
    CONF_ID,
    CONF_LAMBDA,
    CONF_MODEL,
    CONF_PAGES,
    CONF_RESET_DURATION,
    CONF_RESET_PIN,
)

DEPENDENCIES = ["spi"]

waveshare_epaper_ns = cg.esphome_ns.namespace("waveshare_epaper")
WaveshareEPaperBase = waveshare_epaper_ns.class_(
    "WaveshareEPaperBase", cg.PollingComponent, spi.SPIDevice, display.DisplayBuffer
)
WaveshareEPaper = waveshare_epaper_ns.class_(
    "WaveshareEPaper", WaveshareEPaperBase
)
WaveshareEPaperBWR = waveshare_epaper_ns.class_(
    "WaveshareEPaperBWR", WaveshareEPaperBase
)
GDEY075T7 = waveshare_epaper_ns.class_(
    "GDEY075T7", WaveshareEPaper
)


MODELS = {
    # Only the reTerminal E1001's panel is vendored; upstream has more models.
    "gdey075t7": ("c", GDEY075T7),
}

# Add proper logger
_LOGGER = logging.getLogger(__name__)

def validate_full_update_every_only_types_ac(value):
    if CONF_FULL_UPDATE_EVERY not in value:
        return value
    if MODELS[value[CONF_MODEL]][0] == "b":
        full_models = []
        for key, val in sorted(MODELS.items()):
            if val[0] != "b":
                full_models.append(key)
        _LOGGER.warning(
            "The 'full_update_every' option is only officially supported for models: %s. "
            "For model %s, this setting may not work as expected.",
            ", ".join(full_models), value[CONF_MODEL]
        )
    return value

CONFIG_SCHEMA = cv.All(
    display.FULL_DISPLAY_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(WaveshareEPaperBase),
            cv.Required(CONF_DC_PIN): pins.gpio_output_pin_schema,
            cv.Required(CONF_MODEL): cv.one_of(*MODELS, lower=True),
            cv.Required(CONF_RESET_PIN): pins.gpio_output_pin_schema,
            cv.Required(CONF_BUSY_PIN): pins.gpio_input_pin_schema,
            cv.Optional(CONF_FULL_UPDATE_EVERY): cv.int_range(min=1, max=4294967295),
            cv.Optional(CONF_RESET_DURATION): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(max=core.TimePeriod(milliseconds=500)),
            ),
        }
    )
    .extend(cv.polling_component_schema("1s"))
    .extend(spi.spi_device_schema()),
    validate_full_update_every_only_types_ac,
    cv.has_at_most_one_key(CONF_PAGES, CONF_LAMBDA),
)

FINAL_VALIDATE_SCHEMA = spi.final_validate_device_schema(
    "waveshare_epaper", require_miso=False, require_mosi=True
)

async def to_code(config):
    model_type, model = MODELS[config[CONF_MODEL]]
    if model_type in ("b", "c"):
        rhs = model.new()
        var = cg.Pvariable(config[CONF_ID], rhs, model)
    else:
        raise NotImplementedError()

    await display.register_display(var, config)
    await spi.register_spi_device(var, config)

    dc = await cg.gpio_pin_expression(config[CONF_DC_PIN])
    cg.add(var.set_dc_pin(dc))

    if CONF_LAMBDA in config:
        lambda_ = await cg.process_lambda(
            config[CONF_LAMBDA], [(display.DisplayRef, "it")], return_type=cg.void
        )
        cg.add(var.set_writer(lambda_))
    if CONF_RESET_PIN in config:
        reset = await cg.gpio_pin_expression(config[CONF_RESET_PIN])
        cg.add(var.set_reset_pin(reset))
    if CONF_BUSY_PIN in config:
        reset = await cg.gpio_pin_expression(config[CONF_BUSY_PIN])
        cg.add(var.set_busy_pin(reset))
    if CONF_FULL_UPDATE_EVERY in config and model_type in ("a", "c"):
        cg.add(var.set_full_update_every(config[CONF_FULL_UPDATE_EVERY]))
    if CONF_RESET_DURATION in config:
        cg.add(var.set_reset_duration(config[CONF_RESET_DURATION]))