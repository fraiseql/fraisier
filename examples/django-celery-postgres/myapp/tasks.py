"""Celery tasks for the example app."""

from celery import shared_task


@shared_task
def process_task(task_id: int) -> str:
    """Example Celery task that processes a task record."""
    from .models import Task

    task = Task.objects.get(id=task_id)
    task.completed = True
    task.save()
    return f"Processed: {task.title}"
