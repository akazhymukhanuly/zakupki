from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.auth import LoginView, health

admin.site.site_header = "Закупки — администрирование"
admin.site.site_title = "Закупки"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", LoginView.as_view(), name="login"),
    path("health/", health, name="health"),
    path("password/", auth_views.PasswordChangeView.as_view(template_name="core/password_change.html",
                                                             success_url="/password/done/"), name="password_change"),
    path("password/done/", auth_views.PasswordChangeDoneView.as_view(template_name="core/password_done.html"),
         name="password_change_done"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
