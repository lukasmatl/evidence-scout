namespace FlightPlan.Api.Contracts;

public sealed record OptimizeFlightPlanResponse(
    string FlightPlanId,
    string Origin,
    string Destination,
    int EstimatedMinutes);
