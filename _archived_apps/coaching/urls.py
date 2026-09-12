"""
URL routing for Coaching Layer app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    FitnessAssessmentViewSet,
    ExerciseViewSet,
    WorkoutProgramViewSet,
    FoodItemViewSet,
    NutritionPlanViewSet,
)

router = DefaultRouter()
router.register('assessments', FitnessAssessmentViewSet, basename='coaching-assessments')
router.register('exercises', ExerciseViewSet, basename='coaching-exercises')
router.register('programs', WorkoutProgramViewSet, basename='coaching-programs')
router.register('food-database', FoodItemViewSet, basename='coaching-food-database')
router.register('nutrition', NutritionPlanViewSet, basename='coaching-nutrition')

urlpatterns = [
    path('', include(router.urls)),
]
