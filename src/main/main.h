void UpdateSelectedAircraftUI(void);

float GetRadarLat(void);
float GetRadarLon(void);
float GetRadarRange(void);
float GetRadarPoll(void);

void SetRadarSettings(
    float lat,
    float lon,
    float rangeKm,
    float pollSeconds);

void SaveRadarSettings(
    float lat,
    float lon,
    float rangeKm,
    float pollSeconds);
