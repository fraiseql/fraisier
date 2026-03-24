"""Minimal Django model for the example app."""

from typing import ClassVar

from django.db import models


class Task(models.Model):
    """A simple task model to demonstrate migrations."""

    title = models.CharField(max_length=200)
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self):
        return self.title
