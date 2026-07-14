#include <math.h>
#include <stdint.h>

#include "bme280.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "i2c_bus.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sendmy_carrier.h"
#include "sendmy_link.h"

static const char *TAG = "espsend";

// BME280 wiring: the sensor runs in I2C mode with SDA on GPIO5 and SCL on
// GPIO6. Its SDO pin is left floating, so it answers on the default 7-bit
// address (BME280_I2C_ADDRESS_DEFAULT, 0x76).
#define I2C_PORT     I2C_NUM_0
#define I2C_SDA_GPIO 5
#define I2C_SCL_GPIO 6
#define I2C_FREQ_HZ  100000

// How long each octet is advertised before advancing to the next message_id.
// The relay network needs time to observe a beacon and file a location report,
// so a real deployment would use minutes here; this is a demo-friendly value.
#define OCTET_WINDOW_MS (60 * 1000)

// The BME280 handle, set up once by init_sensor() before transmission starts.
static bme280_handle_t s_bme280;

// Unilink ID, shared out of band with the receiver. Provisioned into NVS
// (namespace "sendmy", key "uid", 32 raw bytes) by the project's NVS scripts.
static uint8_t s_uid[SM_CR_UID_LEN];

// Load the Unilink ID from the "sendmy" NVS namespace into out.
static esp_err_t load_uid(uint8_t out[SM_CR_UID_LEN])
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("sendmy", NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open(sendmy): %s", esp_err_to_name(err));
        return err;
    }

    size_t len = SM_CR_UID_LEN;
    err = nvs_get_blob(handle, "uid", out, &len);
    nvs_close(handle);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_get_blob(uid): %s", esp_err_to_name(err));
        return err;
    }
    if (len != SM_CR_UID_LEN) {
        ESP_LOGE(TAG, "uid blob is %u bytes, expected %u", (unsigned)len, SM_CR_UID_LEN);
        return ESP_ERR_INVALID_SIZE;
    }
    return ESP_OK;
}

// Bring up the I2C bus and the BME280 sitting on it. On success s_bme280 holds
// a usable sensor handle.
static esp_err_t init_sensor(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_SDA_GPIO,
        .scl_io_num = I2C_SCL_GPIO,
        .sda_pullup_en = true,
        .scl_pullup_en = true,
        .master.clk_speed = I2C_FREQ_HZ,
    };

    i2c_bus_handle_t bus = i2c_bus_create(I2C_PORT, &conf);
    if (bus == NULL) {
        ESP_LOGE(TAG, "i2c_bus_create failed");
        return ESP_FAIL;
    }

    s_bme280 = bme280_create(bus, BME280_I2C_ADDRESS_DEFAULT);
    if (s_bme280 == NULL) {
        ESP_LOGE(TAG, "bme280_create failed");
        i2c_bus_delete(&bus);
        return ESP_FAIL;
    }

    esp_err_t err = bme280_default_init(s_bme280);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bme280_default_init: %s", esp_err_to_name(err));
        return err;
    }

    // bme280_default_init() leaves the sensor in continuous NORMAL mode, which
    // measures forever and is the worst case for power. We only need one reading
    // per key rotation, so switch to FORCED mode: the chip takes a single
    // measurement on demand (bme280_take_forced_measurement) and then drops back
    // to sleep, its lowest-power state, on its own. Temperature is oversampled
    // x1 and pressure/humidity are skipped, since the carrier only ever carries
    // temperature. (Standby duration is irrelevant outside NORMAL mode.)
    err = bme280_set_sampling(s_bme280, BME280_MODE_FORCED, BME280_SAMPLING_X1,
                              BME280_SAMPLING_NONE, BME280_SAMPLING_NONE,
                              BME280_FILTER_OFF, BME280_STANDBY_MS_0_5);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bme280_set_sampling: %s", esp_err_to_name(err));
        return err;
    }
    return ESP_OK;
}

// Pack a temperature in degrees Celsius into the single octet a carrier can
// hold. We round to the nearest whole degree and send it as a two's-complement
// signed byte (-128..127 C), which spans any realistic ambient reading; the
// receiver reads the octet back as a signed degree count.
static uint8_t encode_temperature(float celsius)
{
    long degrees = lroundf(celsius);
    if (degrees > INT8_MAX) {
        degrees = INT8_MAX;
    } else if (degrees < INT8_MIN) {
        degrees = INT8_MIN;
    }
    return (uint8_t)(int8_t)degrees;
}

// Continuously samples the BME280 and advertises one carrier per reading.
// message_id is a monotonic 32-bit counter that advances forever; the payload
// octet is the current temperature encoded by encode_temperature().
static void sender_task(void *arg)
{
    for (uint32_t mid = 0;; mid++) {
        // Wake the sleeping sensor for a single forced measurement; it returns
        // to sleep once the reading is latched, then we pull it out.
        float celsius = 0.0f;
        esp_err_t err = bme280_take_forced_measurement(s_bme280);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "forced measurement mid=%lu: %s", (unsigned long)mid, esp_err_to_name(err));
            vTaskDelay(pdMS_TO_TICKS(OCTET_WINDOW_MS));
            continue;
        }
        err = bme280_read_temperature(s_bme280, &celsius);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "read temperature mid=%lu: %s", (unsigned long)mid, esp_err_to_name(err));
            vTaskDelay(pdMS_TO_TICKS(OCTET_WINDOW_MS));
            continue;
        }

        uint8_t payload = encode_temperature(celsius);
        uint8_t carrier[SM_CR_CARRIER_LEN];

        err = sm_cr_build_carrier(s_uid, mid, payload, carrier);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "build carrier mid=%lu: %s", (unsigned long)mid, esp_err_to_name(err));
            continue;
        }

        // A 28-octet carrier is exactly an OF advertising key.
        ESP_ERROR_CHECK(sm_ll_set_key(carrier));
        ESP_LOGI(TAG, "tx mid=%lu temp=%.2fC payload=0x%02x", (unsigned long)mid, celsius, payload);

        vTaskDelay(pdMS_TO_TICKS(OCTET_WINDOW_MS));
    }
}

// Called once the NimBLE host has synced and is ready to advertise.
static void on_ready(void)
{
    ESP_LOGI(TAG, "BLE host ready, starting transmission");
    xTaskCreate(sender_task, "sendmy_tx", 4096, NULL, 5, NULL);
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    if (load_uid(s_uid) != ESP_OK) {
        ESP_LOGE(TAG, "no UID provisioned; flash the sendmy NVS partition first");
        return;  // nothing to send without a UID
    }

    if (init_sensor() != ESP_OK) {
        ESP_LOGE(TAG, "BME280 init failed; check wiring (SDA=GPIO5, SCL=GPIO6)");
        return;  // nothing to send without a sensor
    }

    // Bring up NimBLE; on_ready fires after host sync, advertising every 1000 ms.
    ESP_ERROR_CHECK(sm_ll_init(on_ready, 1000));
}
