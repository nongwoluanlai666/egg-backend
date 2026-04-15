from django.conf.urls import url

from wxcloudrun import views


urlpatterns = (
    url(r'^api/count(/)?$', views.counter),
    url(r'^api/egg-feedback(/)?$', views.egg_feedback),
    url(r'^(/)?$', views.index),
)
