"""
Manual demo for the citybrain MCP server: starts it as a subprocess,
lists its tools, calls each one, and prints the results.

Run: python mcp_server/demo.py
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"Discovered {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description.splitlines()[0]}")
            print()

            calls = [
                ("fuse_evidence", {
                    "items": [
                        {"likelihood_ratio": 1e300, "weight": 1.0, "group_id": 0},
                    ],
                    "lr_cap": 8.0, "prior": 0.02,
                }),
                ("fault_tolerance_bound", {
                    "lr_cap": 8.0, "prior": 0.02, "threshold": 0.20,
                }),
                ("detect_change_points", {
                    "values": [0.0, 0.1, -0.2, 0.0, 3.0, 3.1, 3.2, 3.0, 3.1] * 3,
                    "warmup_samples": 5,
                }),
                ("graph_adjust_prior", {
                    "target_category": 1, "active_categories": [0], "prior": 0.02,
                }),
                ("graph_has_unrelated_pair", {
                    "active_categories": [0, 1, 2],
                }),
                ("evaluate_decision", {"posterior": 0.3}),
                ("physics_flood_expected_rise_rate", {
                    "area_m2": 1000.0, "inflow_m3_per_s": 5.0,
                    "drainage_coefficient": 0.5, "drainage_exponent": 1.5,
                    "water_level_m": 2.0,
                }),
                ("physics_fire_growth_class_alpha", {"growth_class": "medium"}),
                ("physics_fire_heat_release_rate", {
                    "alpha_kw_per_s2": 0.0117, "time_s": 120.0,
                }),
                ("physics_plume_concentration", {
                    "emission_rate": 10.0, "wind_speed_m_s": 2.0,
                    "sigma_y_m": 50.0, "sigma_z_m": 20.0,
                    "crosswind_offset_m": 0.0, "receptor_height_m": 0.0,
                    "effective_source_height_m": 0.0,
                }),
                ("physics_traffic_expected_speed", {
                    "free_flow_speed": 30.0, "jam_density": 120.0, "density": 60.0,
                }),
                ("physics_gaussian_likelihood_ratio", {
                    "observed": 5.0, "expected_h1": 8.0, "expected_h0": 0.0, "sigma": 2.0,
                }),
            ]

            for name, args in calls:
                print(f"--- call: {name}({json.dumps(args)}) ---")
                result = await session.call_tool(name, args)
                for block in result.content:
                    if hasattr(block, "text"):
                        print(block.text)
                print()


if __name__ == "__main__":
    asyncio.run(main())
