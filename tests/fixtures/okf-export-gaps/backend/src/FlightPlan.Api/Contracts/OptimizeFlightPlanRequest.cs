namespace FlightPlan.Api.Contracts;

public sealed record OptimizeFlightPlanRequest(
    string AircraftId,
    string Origin,
    string Destination);
