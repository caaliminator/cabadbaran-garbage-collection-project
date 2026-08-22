"""
Socket.IO event handlers.

Two rules run through all of this, both from spec section 8:

  * **Room joins are validated against the session role.** The client never
    names the rooms it wants; the server works them out from the account and
    joins them on connect. A browser cannot subscribe to a barangay it has no
    business seeing by editing one line of JavaScript.

  * **A location update is accepted only from the collector it belongs to.**
    The user id comes from the session, and any id in the payload is ignored --
    otherwise one collector could drive another's marker around the map.

Handlers mirror the REST endpoints rather than replacing them. If the socket
is down the client polls every 30 seconds and nothing is lost; if it is up,
the same data arrives sooner.
"""

from flask import request, session
from flask_socketio import disconnect, emit, join_room

from services import auth_service, duty_service, realtime, storage, triggers


def register(socketio) -> None:
    """Attach every handler. Called once from the app factory."""

    def _session_user() -> dict | None:
        """
        Resolve the socket's session to a live account.

        Read fresh on every event, exactly as the HTTP guard does: an account
        deactivated mid-shift stops being able to push its position on the
        very next message rather than whenever it reconnects.
        """
        user_id = session.get("user_id")
        if not user_id:
            return None
        record = auth_service.by_id(user_id)
        if not record or record.get("status") != "Active":
            return None
        return auth_service.public_view(record)

    @socketio.on("connect")
    def on_connect(auth=None):
        """
        Join the rooms this session is entitled to, and no others.

        Anonymous connections are allowed and get `public` only -- the public
        viewer's live map depends on it.
        """
        user = _session_user()
        rooms = realtime.rooms_for(user)
        for room in rooms:
            join_room(room)

        emit("joined", {
            "rooms": rooms,
            "role": (user or {}).get("role"),
            "authenticated": bool(user),
        })

    @socketio.on("rejoin")
    def on_rejoin(_data=None):
        """
        After a reconnect. The client asks to be re-roomed but does not say
        into what -- the server decides again from the session.
        """
        on_connect()

    @socketio.on("location_update")
    def on_location_update(data):
        """
        A collector's phone reporting its position, every few seconds while on
        duty.

        Ignores any id in the payload: the sender is whoever the session says
        it is. Positions from someone not on duty are dropped, so a phone left
        running after a shift cannot keep a stale marker alive.
        """
        user = _session_user()
        if not user or user.get("role") not in auth_service.COLLECTOR_ROLES:
            return

        payload = data or {}
        updated = duty_service.record_location(
            user["id"], payload.get("lat"), payload.get("lng"),
            payload.get("accuracy"))
        if not updated:
            emit("location_rejected", {"reason": "not on duty or invalid position"})
            return

        position = updated["last_location"]
        realtime.location_update(user, position["lat"], position["lng"])

        # Proximity to an MRF is checked here rather than on a timer: this is
        # the only moment the truck's position actually changes.
        try:
            triggers.check_truck_approaching(user, position["lat"], position["lng"])
        except Exception:                   # pragma: no cover - defensive
            # A failed alert must not cost the position update that carried it.
            pass

    @socketio.on("collector_status")
    def on_collector_status(data):
        """On Duty / Off Duty from the collector's own toggle."""
        user = _session_user()
        if not user or user.get("role") not in auth_service.COLLECTOR_ROLES:
            return

        going_on = bool((data or {}).get("on_duty"))
        duty_service.set_duty(user["id"], going_on)
        realtime.collector_status(user, going_on)

    @socketio.on("mark_notification_read")
    def on_mark_read(data):
        from services import notification_service

        user = _session_user()
        if not user:
            return
        target = (data or {}).get("id")
        if target:
            notification_service.mark_read(target, user["id"])
        else:
            notification_service.mark_all_read(user)
        emit("notification_count",
             {"unread": notification_service.unread_count(user)})

    @socketio.on("disconnect")
    def on_disconnect(_reason=None):
        # Nothing to clean up: rooms are per-connection and Socket.IO drops
        # them. Duty state deliberately survives a dropped connection, since
        # a tunnel or a locked screen is not the end of a shift.
        return

    @socketio.on_error_default
    def on_error(exc):                      # pragma: no cover - defensive
        """Never let a handler error take the whole socket server down."""
        from flask import current_app
        current_app.logger.exception("Socket handler failed: %s", exc)
