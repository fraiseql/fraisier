"""URL configuration for the example app."""

from django.urls import path

from . import views

urlpatterns = [
    path("health", views.health),
    path("tasks", views.task_list),
    path("tasks/create", views.task_create),
]
