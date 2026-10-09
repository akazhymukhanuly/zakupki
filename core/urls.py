from django.urls import path

from .views import contracts, dashboard, memos, pool, procurements, refs, workspace as ws
from .views.common import honor_next as hn

urlpatterns = [
    path("", ws.home, name="home"),
    path("ws/new/", ws.new_memo, name="ws_new_memo"),
    path("ws/<str:key>/ask/", ws.ask, name="ws_ask"),
    path("ws/<str:key>/comment/", ws.comment, name="ws_comment"),
    path("ws/<str:key>/attach/", ws.attach, name="ws_attach"),
    path("ws/answer/<int:pk>/", ws.answer, name="ws_answer"),
    path("ws/approve/<int:pk>/", ws.approve, name="ws_approve"),
    path("ws/approve-green/", ws.approve_green, name="ws_approve_green"),
    path("ws/proc/<int:pk>/quick-price/", ws.quick_price, name="ws_quick_price"),
    path("ws/proc/<int:pk>/submit/", ws.submit_decision, name="ws_submit_decision"),
    path("ws/export.xlsx", ws.export, name="ws_export"),
    path("files/<int:pk>/", ws.download, name="attachment"),
    path("demo/switch/", ws.demo_switch, name="demo_switch"),
    path("access/", ws.access_matrix, name="access_matrix"),
    path("dashboard/", dashboard.dashboard, name="dashboard"),
    path("dashboard/periodic/", dashboard.run_periodic, name="run_periodic"),
    path("approvals/", memos.approvals, name="approvals"),

    # СЗ
    path("memos/", memos.memo_list, name="memo_list"),
    path("memos/new/", memos.memo_create, name="memo_create"),
    path("memos/<int:pk>/", memos.memo_detail, name="memo_detail"),
    path("memos/<int:pk>/edit/", memos.memo_edit, name="memo_edit"),
    path("memos/<int:pk>/print/", memos.memo_print, name="memo_print"),
    path("memos/<int:pk>/items/new/", memos.item_edit, name="item_add"),
    path("memos/<int:pk>/items/<int:item_pk>/", memos.item_edit, name="item_edit"),
    path("memos/<int:pk>/items/<int:item_pk>/delete/", memos.item_delete, name="item_delete"),
    path("memos/<int:pk>/<str:action>/", hn(memos.memo_action), name="memo_action"),
    path("items/<int:item_pk>/<str:action>/", hn(memos.item_action), name="item_action"),
    path("analog/<int:pk>/", hn(memos.analog_confirm), name="analog_confirm"),

    # Пул и закупки
    path("pool/", pool.pool, name="pool"),
    path("pool/create/", pool.pool_create_procurement, name="pool_create_procurement"),
    path("procurements/", procurements.procurement_list, name="procurement_list"),
    path("procurements/<int:pk>/", procurements.procurement_detail, name="procurement_detail"),
    path("procurements/<int:pk>/compare.xlsx", procurements.comparison_export, name="comparison_export"),
    path("procurements/<int:pk>/rfq/<int:rfq_pk>/quote/", procurements.quote_entry, name="quote_entry"),
    path("procurements/<int:pk>/rfq/<int:rfq_pk>/template.xlsx", procurements.quote_template, name="quote_template"),
    path("procurements/<int:pk>/rfq/<int:rfq_pk>/print/", procurements.rfq_print, name="rfq_print"),
    path("procurements/<int:pk>/do/<str:action>/", hn(procurements.procurement_action), name="procurement_action"),
    path("decision-approval/<int:pk>/", hn(procurements.decision_approve), name="decision_approve"),

    # Договоры и 1С
    path("contracts/", contracts.contract_list, name="contract_list"),
    path("contracts/new/", contracts.contract_create, name="contract_create"),
    path("contracts/<int:pk>/", contracts.contract_detail, name="contract_detail"),
    path("contracts/<int:pk>/do/<str:action>/", hn(contracts.contract_action), name="contract_action"),
    path("integration/", contracts.integration, name="integration"),
    path("integration/template/<str:kind>/", contracts.integration_template, name="integration_template"),

    # Справочники, отчёты, прочее
    path("suppliers/", refs.supplier_list, name="supplier_list"),
    path("suppliers/new/", refs.supplier_edit, name="supplier_create"),
    path("import/", refs.refs_import, name="refs_import"),
    path("import/template.xlsx", refs.refs_template, name="refs_template"),
    path("suppliers/<int:pk>/", refs.supplier_edit, name="supplier_edit"),
    path("reports/", dashboard.reports, name="reports"),
    path("notifications/", dashboard.notifications, name="notifications"),
    path("notifications/<int:pk>/", dashboard.notification_open, name="notification_open"),
    path("notifications/read-all/", dashboard.notifications_read_all, name="notifications_read_all"),
    path("rights/", dashboard.rights, name="rights"),

    # Портал поставщика (без авторизации, по токену)
    path("kp/<str:token>/", dashboard.portal_quote, name="portal_quote"),
]
