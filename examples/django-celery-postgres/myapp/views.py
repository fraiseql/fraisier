"""Minimal Django views for the example app."""

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import Task


@require_GET
def health(_request):
    """Health check endpoint for Fraisier."""
    return JsonResponse({"status": "ok"})


@require_GET
def task_list(_request):
    """List all tasks."""
    tasks = list(Task.objects.values("id", "title", "completed", "created_at"))
    return JsonResponse({"tasks": tasks}, safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def task_create(request):
    """Create a new task."""
    data = json.loads(request.body)
    task = Task.objects.create(title=data["title"])
    return JsonResponse(
        {"id": task.id, "title": task.title, "completed": task.completed},
        status=201,
    )
