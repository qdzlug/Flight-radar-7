void UpdateSelectedAircraftUI(void);

float GetRadarLat(void);
float GetRadarLon(void);
float GetRadarRange(void);
float GetRadarPoll(void);
float GetAircraftCycle(void);
void ResetAircraftCycleTimer(void);

void SetRadarSettings(
    float lat,
    float lon,
    float rangeKm,
    float pollSeconds,
    float cycleSeconds);

void SaveRadarSettings(
    float lat,
    float lon,
    float rangeKm,
    float pollSeconds,
    float cycleSeconds);
