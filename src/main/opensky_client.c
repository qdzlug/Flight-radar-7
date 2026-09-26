#include "opensky_client.h"

#include <string.h>
#include <time.h>

#include "esp_http_client.h"
#include "esp_log.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "nvs.h"
#include "nvs_flash.h"

#include "cJSON.h"

#include "esp_crt_bundle.h"

#define OPENSKY_NAMESPACE "opensky"
#define RADAR_NAMESPACE "radar"

static void LoadDataUrl(void);

extern const uint8_t isrgrootx1_pem_start[] asm("_binary_isrgrootx1_pem_start");
extern const uint8_t isrgrootx1_pem_end[] asm("_binary_isrgrootx1_pem_end");

static const char *TAG = "OpenSky";

static char clientId[128];
static char clientSecret[256];

static char accessToken[2048];

static time_t tokenExpiry = 0;

static char *responseBuffer = NULL;
static size_t responseLength = 0;
static size_t responseCapacity = 0;

#define MAX_AIRCRAFT 200

Aircraft gAircraft[MAX_AIRCRAFT];
int gAircraftCount = 0;

bool OpenSky_ParseAircraft(
    const char *json)
{
    gAircraftCount = 0;

    cJSON *root =
        cJSON_Parse(json);

    if (!root)
        return false;

    cJSON *states =
        cJSON_GetObjectItem(
            root,
            "states");

    if (!cJSON_IsArray(states))
    {
        cJSON_Delete(root);
        return false;
    }

    int count =
        cJSON_GetArraySize(states);

    for (int i = 0;
         i < count &&
         gAircraftCount < MAX_AIRCRAFT;
         i++)
    {
        cJSON *state =
            cJSON_GetArrayItem(
                states,
                i);

        if (!cJSON_IsArray(state))
            continue;

        Aircraft *a =
            &gAircraft[gAircraftCount];

        memset(
            a,
            0,
            sizeof(Aircraft));

        cJSON *icao =
            cJSON_GetArrayItem(state, 0);

        cJSON *callsign =
            cJSON_GetArrayItem(state, 1);

        cJSON *lon =
            cJSON_GetArrayItem(state, 5);

        cJSON *lat =
            cJSON_GetArrayItem(state, 6);

        cJSON *vel =
            cJSON_GetArrayItem(state, 9);

        cJSON *hdg =
            cJSON_GetArrayItem(state, 10);

        cJSON *alt =
            cJSON_GetArrayItem(state, 13);
        cJSON *category =
            cJSON_GetArrayItem(state, 17);

        cJSON *acType =
            cJSON_GetArrayItem(state, 18);

        cJSON *acReg =
            cJSON_GetArrayItem(state, 19);

        cJSON *baroAlt =
            cJSON_GetArrayItem(state, 7);

        cJSON *onGround =
            cJSON_GetArrayItem(state, 8);

        cJSON *vRate =
            cJSON_GetArrayItem(state, 11);

        cJSON *country =
            cJSON_GetArrayItem(state, 2);

        if (!icao ||
            !lat ||
            !lon)
        {
            continue;
        }

        if (onGround &&
            cJSON_IsBool(onGround) &&
            cJSON_IsTrue(onGround))
        {
            continue;
        }

        if (country &&
            cJSON_IsString(country))
        {
            strncpy(
                a->originCountry,
                country->valuestring,
                sizeof(a->originCountry) - 1);
        }

        a->category = 0;

        if (category &&
            cJSON_IsNumber(category))
        {
            a->category =
                category->valueint;
        }

        if (acType &&
            cJSON_IsString(acType))
        {
            strncpy(
                a->type,
                acType->valuestring,
                sizeof(a->type) - 1);
        }

        if (acReg &&
            cJSON_IsString(acReg))
        {
            strncpy(
                a->reg,
                acReg->valuestring,
                sizeof(a->reg) - 1);
        }

        strncpy(
            a->icao24,
            icao->valuestring,
            sizeof(a->icao24) - 1);

        if (callsign &&
            cJSON_IsString(callsign))
        {
            strncpy(
                a->callsign,
                callsign->valuestring,
                sizeof(a->callsign) - 1);
        }

        if (cJSON_IsNumber(lat))
            a->latitude = lat->valuedouble;

        if (cJSON_IsNumber(lon))
            a->longitude = lon->valuedouble;

        if (cJSON_IsNumber(vel))
            a->velocity = vel->valuedouble;

        if (cJSON_IsNumber(hdg))
            a->heading = hdg->valuedouble;

        if (cJSON_IsNumber(alt))
            a->altitude = alt->valuedouble;
        else if (cJSON_IsNumber(baroAlt))
            a->altitude = baroAlt->valuedouble;

        if (cJSON_IsNumber(vRate))
            a->verticalRate = vRate->valuedouble;

        a->valid = true;

        a->latitude = lat->valuedouble;
        a->longitude = lon->valuedouble;

        a->predictedLat = a->latitude;
        a->predictedLon = a->longitude;

        a->lastUpdateMs = xTaskGetTickCount() *
                          portTICK_PERIOD_MS;

        gAircraftCount++;
    }

    cJSON_Delete(root);

    return true;
}

static esp_err_t HttpEventHandler(esp_http_client_event_t *evt)
{
    switch (evt->event_id)
    {
    case HTTP_EVENT_ON_DATA:
        // REMOVED the strict non-chunked check because chunked responses are common!
        if (responseLength + evt->data_len + 1 > responseCapacity)
        {
            return ESP_FAIL;
        }
        memcpy(responseBuffer + responseLength, evt->data, evt->data_len);
        responseLength += evt->data_len;
        responseBuffer[responseLength] = '\0';
        break;
    default:
        break;
    }
    return ESP_OK;
}

static bool LoadCredentials(void)
{
    nvs_handle_t handle;

    esp_err_t err =
        nvs_open(
            OPENSKY_NAMESPACE,
            NVS_READONLY,
            &handle);

    ESP_LOGI(TAG,
             "LoadCredentials nvs_open=%s",
             esp_err_to_name(err));

    if (err != ESP_OK)
    {
        return false;
    }

    size_t idLen =
        sizeof(clientId);

    size_t secretLen =
        sizeof(clientSecret);

    esp_err_t err1 =
        nvs_get_str(
            handle,
            "client_id",
            clientId,
            &idLen);

    esp_err_t err2 =
        nvs_get_str(
            handle,
            "client_secret",
            clientSecret,
            &secretLen);

    ESP_LOGI(TAG,
             "client_id=%s",
             esp_err_to_name(err1));

    ESP_LOGI(TAG,
             "client_secret=%s",
             esp_err_to_name(err2));

    nvs_close(handle);

    return (
        err1 == ESP_OK &&
        err2 == ESP_OK);
}

static bool RequestToken(void)
{

    time_t now;
    time(&now);

    ESP_LOGI(
        TAG,
        "Epoch=%lld",
        (long long)now);

    responseLength = 0;

    char postBody[512];

    snprintf(
        postBody,
        sizeof(postBody),
        "grant_type=client_credentials"
        "&client_id=%s"
        "&client_secret=%s",
        clientId,
        clientSecret);

    esp_http_client_config_t config =
        {
            .url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
            //.url = "https://www.google.com",
            .event_handler = HttpEventHandler,
            .transport_type = HTTP_TRANSPORT_OVER_SSL,
            //.cert_pem = (const char *)isrgrootx1_pem_start,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .timeout_ms = 15000,
            .buffer_size = 8192,
            .buffer_size_tx = 4096,
        };

    esp_http_client_handle_t client =
        esp_http_client_init(
            &config);

    if (!client)
    {
        ESP_LOGE(TAG, "Client init failed (out of memory)");

        return false;
    }

    esp_http_client_set_method(
        client,
        HTTP_METHOD_POST);

    esp_http_client_set_header(
        client,
        "Content-Type",
        "application/x-www-form-urlencoded");

    esp_http_client_set_post_field(
        client,
        postBody,
        strlen(postBody));

    if (esp_http_client_perform(
            client) != ESP_OK)
    {
        esp_http_client_cleanup(
            client);

        return false;
    }

    esp_http_client_cleanup(
        client);

    cJSON *root =
        cJSON_Parse(
            responseBuffer);

    if (!root)
        return false;

    cJSON *token =
        cJSON_GetObjectItem(
            root,
            "access_token");

    cJSON *expires =
        cJSON_GetObjectItem(
            root,
            "expires_in");

    if (!token ||
        !expires)
    {
        cJSON_Delete(root);
        return false;
    }

    strncpy(
        accessToken,
        token->valuestring,
        sizeof(accessToken) - 1);

    tokenExpiry =
        time(NULL) +
        expires->valueint -
        60;

    cJSON_Delete(root);

    ESP_LOGI(
        TAG,
        "Token acquired");

    return true;
}

static bool EnsureToken(void)
{
    time_t now =
        time(NULL);

    if (accessToken[0] &&
        now < tokenExpiry)
    {
        return true;
    }

    return RequestToken();
}

bool OpenSky_Init(void)
{
    responseCapacity = 65536;

    responseBuffer =
        malloc(
            responseCapacity);

    if (!responseBuffer)
        return false;

    responseBuffer[0] = '\0';

    LoadDataUrl();

    return LoadCredentials();
}

/*
 * The data source URL is owned here so the poll task, the web task and the
 * first-boot checks all read the same copy. It is guarded by a mutex because
 * the web server can replace it while the radar is polling.
 */
static char dataUrl[128];
static bool dataUrlLoaded = false;
static SemaphoreHandle_t dataUrlMutex = NULL;

static void DataUrlLock(void)
{
    if (!dataUrlMutex)
    {
        dataUrlMutex = xSemaphoreCreateMutex();
    }

    if (dataUrlMutex)
    {
        xSemaphoreTake(
            dataUrlMutex,
            portMAX_DELAY);
    }
}

static void DataUrlUnlock(void)
{
    if (dataUrlMutex)
    {
        xSemaphoreGive(
            dataUrlMutex);
    }
}

static void LoadDataUrl(void)
{
    char stored[128];

    nvs_handle_t handle;

    stored[0] = '\0';

    if (nvs_open(
            RADAR_NAMESPACE,
            NVS_READONLY,
            &handle) == ESP_OK)
    {
        size_t len = sizeof(stored);

        if (nvs_get_str(
                handle,
                "dataurl",
                stored,
                &len) != ESP_OK)
        {
            stored[0] = '\0';
        }

        nvs_close(handle);
    }

    DataUrlLock();

    strncpy(
        dataUrl,
        stored,
        sizeof(dataUrl) - 1);

    dataUrl[sizeof(dataUrl) - 1] = '\0';

    dataUrlLoaded = true;

    DataUrlUnlock();
}

bool OpenSky_HasDataSource(void)
{
    /*
     * Load on first use: the poll task is gated on this call, so waiting for
     * OpenSky_Init would mean a device configured with only a data source
     * never fetches anything.
     */
    DataUrlLock();

    bool loaded = dataUrlLoaded;

    DataUrlUnlock();

    if (!loaded)
    {
        LoadDataUrl();
    }

    DataUrlLock();

    bool configured = strlen(dataUrl) > 0;

    DataUrlUnlock();

    return configured;
}

bool OpenSky_SetDataUrl(
    const char *url)
{
    nvs_handle_t handle;

    if (!url)
    {
        return false;
    }

    if (nvs_open(
            RADAR_NAMESPACE,
            NVS_READWRITE,
            &handle) != ESP_OK)
    {
        return false;
    }

    esp_err_t err =
        nvs_set_str(
            handle,
            "dataurl",
            url);

    esp_err_t commitErr =
        nvs_commit(handle);

    nvs_close(handle);

    if (err != ESP_OK ||
        commitErr != ESP_OK)
    {
        return false;
    }

    DataUrlLock();

    strncpy(
        dataUrl,
        url,
        sizeof(dataUrl) - 1);

    dataUrl[sizeof(dataUrl) - 1] = '\0';

    dataUrlLoaded = true;

    DataUrlUnlock();

    ESP_LOGI(
        TAG,
        "Data source set to '%s'",
        dataUrl);

    return true;
}

bool OpenSky_HasCredentials(void)
{
    /* Never log the secret: the serial console is not a private channel. */
    ESP_LOGI(
        TAG,
        "OpenSky clientId present: %s",
        strlen(clientId) > 0 ? "yes" : "no");

    return strlen(clientId) > 0 &&
           strlen(clientSecret) > 0;
}

static bool FetchStates(
    const char *url,
    bool withAuth,
    char *buffer,
    size_t bufferSize)
{
    responseLength = 0;
    responseBuffer[0] = '\0';

    char bearer[2200];

    snprintf(
        bearer,
        sizeof(bearer),
        "Bearer %s",
        accessToken);

    ESP_LOGI(TAG, "Request URL: %s", url);

    esp_http_client_config_t config =
        {
            .url = url,
            .event_handler = HttpEventHandler,
            .timeout_ms = 15000,
            .buffer_size = 8192,
            .buffer_size_tx = 4096,
        };

    if (strncmp(url, "https://", 8) == 0)
    {
        config.transport_type = HTTP_TRANSPORT_OVER_SSL;
        config.crt_bundle_attach = esp_crt_bundle_attach;
    }

    esp_http_client_handle_t client =
        esp_http_client_init(&config);

    if (!client)
    {
        ESP_LOGE(TAG, "Client init failed (out of memory)");

        return false;
    }

    esp_http_client_set_method(
        client,
        HTTP_METHOD_GET);

    if (withAuth)
    {
        esp_http_client_set_header(
            client,
            "Authorization",
            bearer);
    }

    esp_err_t err =
        esp_http_client_perform(client);

    if (err != ESP_OK)
    {
        ESP_LOGE(
            TAG,
            "HTTP GET failed: %s",
            esp_err_to_name(err));

        esp_http_client_cleanup(client);
        return false;
    }

    int status =
        esp_http_client_get_status_code(client);

    ESP_LOGI(
        TAG,
        "HTTP Status = %d",
        status);

    esp_http_client_cleanup(client);

    if (status != 200)
    {
        ESP_LOGE(
            TAG,
            "Unexpected HTTP status: %d",
            status);
        return false;
    }

    strncpy(
        buffer,
        responseBuffer,
        bufferSize - 1);

    buffer[bufferSize - 1] = '\0';

    return true;
}

bool OpenSky_GetAircraftJson(
    float minLat,
    float maxLat,
    float minLon,
    float maxLon,
    char *buffer,
    size_t bufferSize)
{
    char url[512];
    char source[128];

    if (OpenSky_HasDataSource())
    {
        DataUrlLock();

        strncpy(
            source,
            dataUrl,
            sizeof(source) - 1);

        source[sizeof(source) - 1] = '\0';

        DataUrlUnlock();

        if (strlen(source) > 0)
        {
            /*
             * A saved URL that already carries a query string would end up
             * with two '?' in a row, so append with '&' instead.
             */
            snprintf(
                url,
                sizeof(url),
                "%s%slamin=%.6f&lamax=%.6f&"
                "lomin=%.6f&lomax=%.6f",
                source,
                strchr(source, '?') ? "&" : "?",
                minLat,
                maxLat,
                minLon,
                maxLon);

            if (FetchStates(url, false, buffer, bufferSize))
            {
                return true;
            }

            ESP_LOGW(
                TAG,
                "Data source '%s' failed, falling back to OpenSky",
                source);
        }
    }

    if (!EnsureToken())
    {
        ESP_LOGE(TAG, "Token unavailable");
        return false;
    }

    snprintf(
        url,
        sizeof(url),
        "https://opensky-network.org/api/states/all?"
        "lamin=%.6f&lamax=%.6f&"
        "lomin=%.6f&lomax=%.6f",
        minLat,
        maxLat,
        minLon,
        maxLon);

    return FetchStates(url, true, buffer, bufferSize);
}
