export type OptimizeFlightPlanRequest = {
  aircraftId: string;
  origin: string;
  destination: string;
};

export type OptimizeFlightPlanResponse = {
  flightPlanId: string;
  estimatedMinutes: number;
};

export async function optimizeFlightPlan(
  request: OptimizeFlightPlanRequest,
): Promise<OptimizeFlightPlanResponse> {
  const response = await fetch("/api/flight-plans/optimize", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error("Flight plan optimization failed");
  }

  return response.json() as Promise<OptimizeFlightPlanResponse>;
}
