import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiohttp import web
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from dotenv import load_dotenv
from microsoft_agents.activity import Activity, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    AuthenticationConstants,
    Authorization,
    ClaimsIdentity,
    MemoryStorage,
    TurnContext,
    TurnState,
)
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, StatusCode

from pmoagent.agent import ReportGenerator
from pmoagent.bot import PmoBot, TokenProvider
from pmoagent.config import Settings
from pmoagent.gpt import GptReportGenerator
from pmoagent.report import parse_command
from pmoagent.store import ConversationStore

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
Work = Callable[[TurnContext], Awaitable[None]]
Defer = Callable[[TurnContext, Work], None]
PUBLIC_PATHS = {"/readiness", "/liveness"}
ACTIVITY_PATHS = {"/activity/messages", "/api/messages"}
MAX_INPUT_CHARS = 2000

# Microsoft 365 Agents SDK settings used by Foundry autopilot agents (see the official sample).
SDK_DEFAULTS = {
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE": "IdentityProxyManager",
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES__0": "5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default",
    "CONNECTIONSMAP__0__SERVICEURL": "*",
    "CONNECTIONSMAP__0__CONNECTION": "SERVICE_CONNECTION",
    "AGENTAPPLICATION__STARTTYPINGTIMER": "false",
    "AGENTAPPLICATION__REMOVERECIPIENTMENTION": "false",
    "AGENTAPPLICATION__NORMALIZEMENTIONS": "false",
    "AGENTAPPLICATION__USERAUTHORIZATION__AUTOSIGNIN": "false",
    "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__TYPE": "AgenticUserAuthorization",
    "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__AGENTIC__SETTINGS__SCOPES": "5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default",
}


def sdk_configuration(environ: dict[str, str]) -> dict[str, Any]:
    values = {**SDK_DEFAULTS, **environ}
    blueprint = environ.get("FOUNDRY_AGENT_BLUEPRINT_CLIENT_ID")
    if blueprint:
        values["CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID"] = blueprint
    config: dict[str, Any] = load_configuration_from_env(values)
    return config


def credential_for(settings: Settings) -> AsyncTokenCredential:
    if settings.environment == "azure":
        return ManagedIdentityCredential(client_id=settings.runtime_client_id)
    return DefaultAzureCredential()


def conversation_key(activity: Activity) -> str:
    conversation = activity.conversation
    tenant = activity.get_agentic_tenant_id() or (conversation.tenant_id if conversation else "")
    instance = activity.get_agentic_instance_id() or "local"
    return f"{instance}:{tenant}:{conversation.id if conversation else ''}"


def teams_location(activity: Activity) -> str:
    data = activity.channel_data if isinstance(activity.channel_data, dict) else {}
    team = data.get("team") or {}
    channel = data.get("channel") or {}
    logger.info(
        "teams_location channel_data=%s keys=%s team_keys=%s",
        type(activity.channel_data).__name__,
        sorted(data),
        sorted(team) if isinstance(team, dict) else type(team).__name__,
    )
    if team.get("aadGroupId") and channel.get("id"):
        return f"Teamsチャネル（team-id: {team['aadGroupId']}, channel-id: {channel['id']}）"
    conversation = activity.conversation
    if conversation and conversation.id:
        return f"Teamsチャット（chat-id: {conversation.id.split(';')[0]}）"
    return ""


def configure_telemetry(environ: dict[str, str]) -> bool:
    # Foundry injects this setting into hosted containers when project monitoring is on.
    connection_string = environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection_string,
        logger_name="pmoagent",
        enable_live_metrics=False,
        resource=Resource.create({"service.name": environ.get("FOUNDRY_AGENT_NAME", "pmo-agent")}),
        instrumentation_options={"httpx2": {"enabled": False}},
    )
    HTTPX2ClientInstrumentor().instrument(async_response_hook=expected_http_response)
    return True


async def expected_http_response(span: Any, request: Any, response: Any) -> None:
    # MCP clients probe an optional GET SSE stream; servers without one answer 405 by spec.
    method = request.method.decode() if isinstance(request.method, bytes) else request.method
    accept = str(request.headers.get("accept", ""))
    if method == "GET" and response.status_code == 405 and "text/event-stream" in accept:
        span.set_status(StatusCode.OK)


def activity_attributes(activity: Activity) -> dict[str, str]:
    attributes = {"activity.id": activity.id or ""}
    if activity.conversation and activity.conversation.id:
        attributes["gen_ai.conversation.id"] = activity.conversation.id
    return attributes


async def prefetch_tokens(
    context: TurnContext,
    scopes: list[str],
    exchange: Callable[[TurnContext, str], Awaitable[str]],
) -> TokenProvider:
    # Agentic token exchange needs the inbound turn, so tokens are acquired before it ends.
    results: dict[str, str | Exception] = {}
    for scope in dict.fromkeys(scopes):
        try:
            results[scope] = await exchange(context, scope)
        except Exception as error:
            results[scope] = error

    async def tokens(scope: str) -> str:
        value = results.get(scope) or RuntimeError(f"No token was prepared for {scope}")
        if isinstance(value, Exception):
            raise value
        return value

    return tokens


async def handle_message(
    context: TurnContext,
    settings: Settings,
    bot: PmoBot,
    exchange: Callable[[TurnContext, str], Awaitable[str]],
    defer: Defer | None = None,
) -> None:
    activity = context.activity
    sender = activity.from_property
    if settings.environment == "azure" and not activity.is_agentic_request():
        await context.send_activity(
            "このエージェントはAutopilotインスタンスとしてのみ利用できます。"
        )
        return
    if sender is None or not sender.aad_object_id:
        await context.send_activity("送信者を確認できないため処理できません。")
        return
    text = (activity.remove_recipient_mention() or activity.text or "").strip()
    if len(text) > MAX_INPUT_CHARS:
        await context.send_activity(f"依頼は{MAX_INPUT_CHARS}文字以内で入力してください。")
        return

    sender_id = sender.aad_object_id
    sender_name = sender.name or "依頼者"
    key = conversation_key(activity)
    location = teams_location(activity)

    async def reply(turn: TurnContext, tokens: TokenProvider) -> None:
        async def typing() -> None:
            while True:
                await turn.send_activity(Activity(type="typing"))
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(typing())
        try:
            answer = await bot.respond(text, sender_id, sender_name, key, tokens, location=location)
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task
        await turn.send_activity(answer)

    if defer is None:

        async def exchange_now(scope: str) -> str:
            return await exchange(context, scope)

        await reply(context, exchange_now)
        return

    command = parse_command(text)
    scopes = [s.scope for s in settings.servers] if command.kind != "approve" else []
    await context.send_activity(Activity(type="typing"))
    tokens = await prefetch_tokens(context, scopes, exchange)

    async def deferred(turn: TurnContext) -> None:
        await reply(turn, tokens)

    defer(context, deferred)


def foundry_gateway_ingress(settings: Settings, environ: dict[str, str]) -> bool:
    return settings.environment == "azure" and bool(environ.get("FOUNDRY_HOSTING_ENVIRONMENT"))


def deferrer(adapter: CloudAdapter, background: set[asyncio.Task[None]]) -> Defer:
    def defer(context: TurnContext, work: Work) -> None:
        # The Foundry gateway drops activity requests held for ~30s, so long work
        # finishes in a proactive turn after the inbound request has returned 202.
        continuation = context.activity.model_copy(deep=True)
        identity = context.identity or ClaimsIdentity({}, authentication_type="Anonymous")

        async def run() -> None:
            # create_task copies the inbound span context, so this span joins the request trace.
            with tracer.start_as_current_span(
                "pmo.reply", attributes=activity_attributes(continuation)
            ) as span:
                try:
                    await adapter.continue_conversation_with_claims(
                        identity,
                        continuation,
                        work,
                        audience=AuthenticationConstants.AGENTS_SDK_SCOPE,
                    )
                except Exception as error:
                    span.record_exception(error)
                    span.set_status(StatusCode.ERROR)
                    logger.exception("Deferred reply failed")

        task = asyncio.create_task(run())
        background.add(task)
        task.add_done_callback(background.discard)

    return defer


def build_app(settings: Settings, environ: dict[str, str] | None = None) -> web.Application:
    environ = dict(environ if environ is not None else os.environ)
    config = sdk_configuration(environ)
    gateway = foundry_gateway_ingress(settings, environ)
    connections = MsalConnectionManager(**config)
    adapter = CloudAdapter(connection_manager=connections)
    sdk_storage = MemoryStorage()
    authorization = Authorization(sdk_storage, connections, **config)
    agent_app = AgentApplication[TurnState](
        storage=sdk_storage, authorization=authorization, **config
    )
    credential = credential_for(settings)
    store = ConversationStore.from_settings(settings, credential)
    generator = (
        GptReportGenerator(settings, credential)
        if settings.provider == "gpt"
        else ReportGenerator(settings)
    )
    bot = PmoBot(settings, store, generator)

    async def exchange(context: TurnContext, scope: str) -> str:
        response = await agent_app.auth.exchange_token(
            context, scopes=[scope], auth_handler_id=settings.auth_handler
        )
        if not response or not response.token:
            raise RuntimeError("Agentic user token exchange returned no token")
        return str(response.token)

    background: set[asyncio.Task[None]] = set()
    defer = deferrer(adapter, background)

    @agent_app.activity("message", auth_handlers=[settings.auth_handler])
    async def on_message(context: TurnContext, _state: TurnState) -> None:
        await handle_message(context, settings, bot, exchange, defer if gateway else None)

    @web.middleware
    async def authenticate(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.method == "GET" and request.path in PUBLIC_PATHS:
            return await handler(request)
        is_activity = request.method == "POST" and request.path in ACTIVITY_PATHS
        with tracer.start_as_current_span(
            f"{request.method} {request.path if is_activity else '(other)'}",
            context=propagate.extract(request.headers),
            kind=SpanKind.SERVER,
        ) as span:
            if gateway and is_activity:
                # Foundry authenticates channel traffic at its edge and does not forward the
                # channel JWT; mirror azure-ai-agentserver-activity's digital-worker claims.
                request["claims_identity"] = ClaimsIdentity({}, authentication_type="Anonymous")
                response = await handler(request)
            else:
                response = await jwt_authorization_middleware(request, handler)
            if not isinstance(response, web.StreamResponse):
                raise TypeError("Activity middleware returned an invalid response")
            span.set_attribute("http.response.status_code", response.status)
            if response.status >= 500:
                span.set_status(StatusCode.ERROR)
        if is_activity:
            logger.info(
                "activity_ingress path=%s mode=%s authorization_present=%s "
                "traceparent_present=%s status=%d",
                request.path,
                "foundry" if gateway else "jwt",
                "Authorization" in request.headers,
                "traceparent" in request.headers,
                response.status,
            )
        return response

    auth_config = connections.get_default_connection_configuration()
    auth_config.ANONYMOUS_ALLOWED = False
    app = web.Application(middlewares=[authenticate], client_max_size=256_000)
    app["agent_configuration"] = auth_config

    async def messages(request: web.Request) -> web.StreamResponse:
        response = await start_agent_process(request, agent_app, adapter)
        return response if response is not None else web.Response(status=202)

    async def ready(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def lifecycle(_: web.Application) -> AsyncIterator[None]:
        try:
            if settings.environment == "local":
                await store.initialize()
            yield
        finally:
            if background:
                await asyncio.wait(background, timeout=settings.generation_timeout)
            await store.close()
            await credential.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/activity/messages", messages)
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/readiness", ready)
    app.router.add_get("/liveness", ready)
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    for name in ("azure", "httpx", "httpcore", "msal"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = Settings()  # type: ignore[call-arg]
    if configure_telemetry(dict(os.environ)):
        logger.info("telemetry exporter=azure_monitor")
    web.run_app(build_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8088")))
