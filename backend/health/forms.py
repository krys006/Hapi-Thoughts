from django import forms

from .models import HealthArticle


class HealthArticleForm(forms.ModelForm):
    """
    Admin form for creating and editing health articles.
    Shared between create and edit views.
    """

    class Meta:
        model = HealthArticle
        fields = [
            "title",
            "description",
            "causes",
            "symptoms",
            "general_care",
            "is_published",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "causes": forms.Textarea(attrs={"rows": 3}),
            "symptoms": forms.Textarea(attrs={"rows": 3}),
            "general_care": forms.Textarea(attrs={"rows": 4}),
        }
