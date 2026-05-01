from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .services import compute_fuel_route


class FuelRouteView(APIView):
    """
    POST /api/route/
    {
        "start": "New York, NY",
        "finish": "Los Angeles, CA"
    }

    Returns optimal fuel stops, total cost, and route geometry.
    """

    def post(self, request):
        start = request.data.get("start", "").strip()
        finish = request.data.get("finish", "").strip()

        if not start or not finish:
            return Response(
                {"error": "Both 'start' and 'finish' fields are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if start.lower() == finish.lower():
            return Response(
                {"error": "'start' and 'finish' must be different locations."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = compute_fuel_route(start, finish)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"error": f"An unexpected error occurred: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
