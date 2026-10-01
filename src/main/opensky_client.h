#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MAX_AIRCRAFT 200

typedef struct
{
    char icao24[12];
    char callsign[16];
    char originCountry[64];
    char type[16];
    char reg[16];
    char flightNumber[16];
    char departureAirport[8];
    char arrivalAirport[8];
    char flightStatus[24];
    char airline[40];
    char enrichmentProvider[16];

    uint32_t estimatedArrival;
    uint32_t enrichmentUpdatedAt;
    bool enrichmentStale;

    int category;

    float longitude;
    float latitude;

    float altitude;
    float velocity;
    float heading;
    float verticalRate;

    bool valid;

    float predictedLat;
    float predictedLon;

    uint32_t lastUpdateMs;

} Aircraft;

extern Aircraft gAircraft[MAX_AIRCRAFT];
extern int gAircraftCount;

bool OpenSky_Init(void);
bool OpenSky_HasCredentials(void);
bool OpenSky_HasDataSource(void);
bool OpenSky_SetDataUrl(const char *url);

bool OpenSky_GetAircraftJson(
    float minLat,
    float maxLat,
    float minLon,
    float maxLon,
    char *buffer,
    size_t bufferSize);

bool OpenSky_ParseAircraft(
    const char *json);
