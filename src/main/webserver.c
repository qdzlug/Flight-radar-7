#include "webserver.h"

#include <string.h>

#include "esp_http_server.h"
#include "esp_log.h"

#include "nvs.h"
#include "nvs_flash.h"

#include "cJSON.h"

#include "opensky_client.h"

static const char *TAG = "WEBSERVER";

#define OPENSKY_NAMESPACE "opensky"

static bool IsValidDataUrl(
    const char *url)
{
    if (!url ||
        strlen(url) < 8)
    {
        return false;
    }

    if (strncmp(url, "http://", 7) != 0 &&
        strncmp(url, "https://", 8) != 0)
    {
        return false;
    }

    /* Reject whitespace and control characters outright. */
    for (const char *p = url; *p; p++)
    {
        if (*p <= ' ' ||
            *p == '"' ||
            *p == '\\')
        {
            return false;
        }
    }

    return true;
}

static esp_err_t SetDataHandler(
    httpd_req_t *req)
{
    int totalLen = req->content_len;

    if (totalLen <= 0 || totalLen > 512)
    {
        httpd_resp_send_err(
            req,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Invalid request");

        return ESP_FAIL;
    }

    char *buffer = malloc(totalLen + 1);

    if (!buffer)
    {
        httpd_resp_send_err(
            req,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Out of memory");

        return ESP_FAIL;
    }

    int received = 0;

    while (received < totalLen)
    {
        int ret = httpd_req_recv(
            req,
            buffer + received,
            totalLen - received);

        if (ret <= 0)
        {
            free(buffer);

            httpd_resp_send_err(
                req,
                HTTPD_500_INTERNAL_SERVER_ERROR,
                "Receive failed");

            return ESP_FAIL;
        }

        received += ret;
    }

    buffer[received] = '\0';

    cJSON *root = cJSON_Parse(buffer);

    free(buffer);

    if (!root)
    {
        httpd_resp_send(
            req,
            "Invalid JSON",
            HTTPD_RESP_USE_STRLEN);

        return ESP_FAIL;
    }

    cJSON *dataUrl =
        cJSON_GetObjectItem(
            root,
            "dataUrl");

    if (!cJSON_IsString(dataUrl))
    {
        cJSON_Delete(root);

        httpd_resp_send(
            req,
            "Missing dataUrl",
            HTTPD_RESP_USE_STRLEN);

        return ESP_FAIL;
    }

    char url[128];

    strncpy(
        url,
        dataUrl->valuestring,
        sizeof(url) - 1);

    url[sizeof(url) - 1] = '\0';

    ESP_LOGI(
        TAG,
        "Data source URL: '%s'",
        url);

    bool saved;

    if (strlen(url) == 0)
    {
        /* An empty field clears the data source and returns to direct OpenSky. */
        saved = OpenSky_SetDataUrl("");
    }
    else if (IsValidDataUrl(url))
    {
        saved = OpenSky_SetDataUrl(url);
    }
    else
    {
        saved = false;
    }

    cJSON_Delete(root);

    if (strlen(url) > 0 &&
        !saved)
    {
        httpd_resp_send(
            req,
            "Invalid URL: expected http:// or https://",
            HTTPD_RESP_USE_STRLEN);

        return ESP_FAIL;
    }

    httpd_resp_send(
        req,
        saved ? "Data source saved"
              : "Save failed",
        HTTPD_RESP_USE_STRLEN);

    return ESP_OK;
}

static bool SaveCredentials(
    const char *clientId,
    const char *clientSecret)
{
    nvs_handle_t handle;

    esp_err_t err =
        nvs_open(
            OPENSKY_NAMESPACE,
            NVS_READWRITE,
            &handle);

    ESP_LOGI(
        TAG,
        "nvs_open=%s",
        esp_err_to_name(err));

    if (err != ESP_OK)
    {
        return false;
    }

    err = nvs_set_str(
        handle,
        "client_id",
        clientId);

    ESP_LOGI(
        TAG,
        "nvs_set_str(client_id)=%s",
        esp_err_to_name(err));

    err = nvs_set_str(
        handle,
        "client_secret",
        clientSecret);

    ESP_LOGI(
        TAG,
        "nvs_set_str(client_secret)=%s",
        esp_err_to_name(err));

    err = nvs_commit(handle);

    ESP_LOGI(
        TAG,
        "nvs_commit=%s",
        esp_err_to_name(err));

    nvs_close(handle);

    return err == ESP_OK;
}

static esp_err_t UploadHandler(
    httpd_req_t *req)
{
    int totalLen = req->content_len;

    if (totalLen <= 0 || totalLen > 2048)
    {
        httpd_resp_send_err(
            req,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Invalid file");

        return ESP_FAIL;
    }

    char *buffer = malloc(totalLen + 1);

    if (!buffer)
    {
        httpd_resp_send_err(
            req,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Out of memory");

        return ESP_FAIL;
    }

    int received = 0;

    while (received < totalLen)
    {
        int ret = httpd_req_recv(
            req,
            buffer + received,
            totalLen - received);

        if (ret <= 0)
        {
            free(buffer);

            httpd_resp_send_err(
                req,
                HTTPD_500_INTERNAL_SERVER_ERROR,
                "Receive failed");

            return ESP_FAIL;
        }

        received += ret;
    }

    buffer[received] = '\0';

    ESP_LOGI(TAG, "Received:\n%s", buffer);

    cJSON *root = cJSON_Parse(buffer);

    free(buffer);

    if (!root)
    {
        httpd_resp_send(
            req,
            "Invalid JSON",
            HTTPD_RESP_USE_STRLEN);

        return ESP_FAIL;
    }

    cJSON *clientId =
        cJSON_GetObjectItem(
            root,
            "clientId");

    cJSON *clientSecret =
        cJSON_GetObjectItem(
            root,
            "clientSecret");

    if (!cJSON_IsString(clientId) ||
        !cJSON_IsString(clientSecret))
    {
        cJSON_Delete(root);

        httpd_resp_send(
            req,
            "Missing clientId/clientSecret",
            HTTPD_RESP_USE_STRLEN);

        return ESP_FAIL;
    }

    SaveCredentials(
        clientId->valuestring,
        clientSecret->valuestring);

    cJSON_Delete(root);

    httpd_resp_send(
        req,
        "Credentials Saved",
        HTTPD_RESP_USE_STRLEN);

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();

    return ESP_OK;
}

static esp_err_t RootHandler(
    httpd_req_t *req)
{
    const char html[] =
        "<!DOCTYPE html>"
        "<html>"
        "<body>"
        "<h2>OpenSky Credentials</h2>"

        "<p>Select credentials.json</p>"

        "<form method='POST' "
        "action='/upload' "
        "enctype='application/octet-stream'>"

        "<input type='file' "
        "id='fileInput'>"

        "<button type='button' "
        "onclick='uploadFile()'>Upload</button>"

        "</form>"

        "<h2>Radar Data Source</h2>"

        "<p>URL of your merge server, e.g. http://192.168.1.50:8000/states/all.<br>"
        "Leave empty to fetch directly from OpenSky.</p>"

        "<input type='text' id='dataUrl' "
        "placeholder='http://192.168.1.50:8000/states/all' "
        "style='width:340px'>"

        "<button type='button' "
        "onclick='saveDataUrl()'>Save Data Source</button>"

        "<script>"
        "async function uploadFile(){"

        "const file="
        "document.getElementById('fileInput').files[0];"

        "if(!file){"
        "alert('Select a file');"
        "return;"
        "}"

        "const data=await file.text();"

        "const response=await fetch('/upload',{"
        "method:'POST',"
        "headers:{"
        "'Content-Type':'application/json'"
        "},"
        "body:data"
        "});"

        "alert(await response.text());"
        "}"

        "async function saveDataUrl(){"

        "const v="
        "document.getElementById('dataUrl').value.trim();"

        "const response=await fetch('/setdata',{"
        "method:'POST',"
        "headers:{"
        "'Content-Type':'application/json'"
        "},"
        "body:JSON.stringify({dataUrl:v})"
        "});"

        "alert(await response.text());"
        "}"
        "</script>"

        "</body>"
        "</html>";

    httpd_resp_set_type(
        req,
        "text/html");

    return httpd_resp_send(
        req,
        html,
        HTTPD_RESP_USE_STRLEN);
}

esp_err_t StartWebServer(void)
{
    httpd_config_t config =
        HTTPD_DEFAULT_CONFIG();

    httpd_handle_t server = NULL;

    if (httpd_start(
            &server,
            &config) != ESP_OK)
    {
        return ESP_FAIL;
    }

    httpd_uri_t root_uri =
        {
            .uri = "/",
            .method = HTTP_GET,
            .handler = RootHandler,
            .user_ctx = NULL};

    httpd_uri_t upload_uri =
        {
            .uri = "/upload",
            .method = HTTP_POST,
            .handler = UploadHandler,
            .user_ctx = NULL};

    httpd_uri_t setdata_uri =
        {
            .uri = "/setdata",
            .method = HTTP_POST,
            .handler = SetDataHandler,
            .user_ctx = NULL};

    httpd_register_uri_handler(
        server,
        &root_uri);

    httpd_register_uri_handler(
        server,
        &upload_uri);

    httpd_register_uri_handler(
        server,
        &setdata_uri);

    ESP_LOGI(
        TAG,
        "Web server started");

    return ESP_OK;
}
