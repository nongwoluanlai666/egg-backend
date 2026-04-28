from django.conf.urls import url

from wxcloudrun import views


urlpatterns = (
    url(r'^api/count(/)?$', views.counter),
    url(r'^api/egg-predict(/)?$', views.egg_predict),
    url(r'^api/egg-feedback(/)?$', views.egg_feedback),
    url(r'^api/merchant-notice/current(/)?$', views.merchant_notice_current),
    url(r'^api/merchant-notice/subscribe-status(/)?$', views.merchant_notice_subscribe_status),
    url(r'^api/merchant-notice/subscribe-next(/)?$', views.merchant_notice_subscribe_next),
    url(r'^api/merchant-notice/reward-unlock(/)?$', views.merchant_notice_reward_unlock),
    url(r'^api/merchant-notice/dev-self-test(/)?$', views.merchant_notice_dev_self_test),
    url(r'^api/merchant-notice/preferences(/)?$', views.merchant_notice_preferences),
    url(r'^api/internal/merchant-watch(/)?$', views.internal_merchant_watch),
    url(r'^api/dev/merchant-notice/broadcast(/)?$', views.dev_merchant_notice_broadcast),
    url(r'^api/dev/feedback-export(/)?$', views.dev_feedback_export),
    url(r'^api/dev/model-config(/)?$', views.dev_model_config),
    url(r'^(/)?$', views.index),
)
