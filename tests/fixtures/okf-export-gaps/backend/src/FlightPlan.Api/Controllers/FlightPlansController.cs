using FlightPlan.Api.Contracts;
using Microsoft.AspNetCore.Mvc;

namespace FlightPlan.Api.Controllers;

[ApiController]
[Route("api/flight-plans")]
public sealed class FlightPlansController : ControllerBase
{
    [HttpPost("optimize")]
    [ProducesResponseType(typeof(OptimizeFlightPlanResponse), StatusCodes.Status200OK)]
    public ActionResult<OptimizeFlightPlanResponse> Optimize(
        [FromBody] OptimizeFlightPlanRequest request)
    {
        return Ok(new OptimizeFlightPlanResponse(
            "fp_123",
            request.Origin,
            request.Destination,
            145));
    }
}
