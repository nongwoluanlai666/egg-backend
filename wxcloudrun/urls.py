from django.conf.urls import url

from wxcloudrun import views


urlpatterns = (
    url(r'^api/count(/)?$', views.counter),
    url(r'^api/egg-predict(/)?$', views.egg_predict),
    url(r'^api/egg-feedback(/)?$', views.egg_feedback),
    url(r'^api/dev/feedback-export(/)?$', views.dev_feedback_export),
    url(r'^api/dev/model-config(/)?$', views.dev_model_config),
    url(r'^(/)?$', views.index),
)
