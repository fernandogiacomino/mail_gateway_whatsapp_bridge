{
    "name": "Mail Gateway WhatsApp Bridge",
    "summary": "Extiende el Gateway WhatsApp para enrutar mensajes a distintos tipos de canales",
    "version": "18.0.1.0.0",
    "author": "Fernando Giacomino para DER",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "mail_gateway_whatsapp",
        "im_livechat"
    ],
    "data": [
        "views/mail_gateway_bridge_views.xml",
        "data/whatsapp_conversation_cron.xml",
        "data/whatsapp_conversation_server_actions.xml",
    ],
    "assets": {
        "im_livechat.assets_embed_core": [
            "mail_gateway_whatsapp_bridge/static/src/embed/common/thread_model_attachment_step_patch.js",
        ],
    },

    "installable": True,
    "auto_install": False,
    "application": False,
}
