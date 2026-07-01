from werkzeug.exceptions import NotFound

from odoo import _
from odoo.exceptions import AccessError
from odoo.http import request, route
from odoo.addons.im_livechat.controllers.attachment import (
    LivechatAttachmentController as BaseLivechatAttachmentController,
)
from odoo.addons.mail.controllers.attachment import AttachmentController

try:
    # Odoo 18+
    from odoo.addons.mail.models.discuss.mail_guest import add_guest_to_context
except ImportError:  # pragma: no cover - compatibility fallback
    from odoo.addons.mail.tools.discuss import add_guest_to_context


class LivechatAttachmentController(BaseLivechatAttachmentController):
    @route()
    @add_guest_to_context
    def mail_attachment_upload(self, ufile, thread_id, thread_model, is_pending=False, **kwargs):
        post_access = request.env[thread_model].sudo()._get_mail_message_access(
            int(thread_id), "create"
        )
        thread = request.env[thread_model]._get_thread_with_access(
            int(thread_id), mode=post_access, **kwargs
        )
        if not thread:
            raise NotFound()

        is_livechat_visitor_upload = (
            thread_model == "discuss.channel"
            and thread.channel_type == "livechat"
            and not request.env.user._is_internal()
        )
        if (
            is_livechat_visitor_upload
            and not thread.livechat_active
            and not thread.sudo()._is_attachment_upload_allowed_for_chatbot_step()
        ):
            raise AccessError(_("You are not allowed to upload attachments on this channel."))

        # Call the base mail upload directly to avoid running the im_livechat guard twice.
        return AttachmentController.mail_attachment_upload(
            self, ufile, thread_id, thread_model, is_pending, **kwargs
        )
