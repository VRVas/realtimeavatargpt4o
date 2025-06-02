# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root for full license information.

import asyncio
import json
import logging
import os
import time
from typing import Tuple

import azure.cognitiveservices.speech as speechsdk
from aiohttp import ClientSession
from data_models import IceServer
from azure_tts import AioOutputStream, InputTextStream, InputTextStreamFromQueue, token_provider

logger = logging.getLogger(__name__)

SPEECH_REGION = os.environ.get("SPEECH_REGION")
SPEECH_KEY = os.environ.get('SPEECH_KEY')
SPEECH_RESOURCE_ID = os.environ.get("SPEECH_RESOURCE_ID")
CNV_DEPLOYMENT_ID = os.environ.get("CNV_DEPLOYMENT_ID", "bfab6398-3c4a-4b85-8e7b-c155ca8bfa50")

if not SPEECH_REGION:
    raise ValueError("SPEECH_REGION must be set")

if not (SPEECH_KEY or SPEECH_RESOURCE_ID):
    raise ValueError("SPEECH_KEY or SPEECH_RESOURCE_ID must be set")


class Client:
    def __init__(self, synthesis_pool_size: int = 2):
        if synthesis_pool_size < 1:
            raise ValueError("synthesis_pool_size must be at least 1")
        self.synthesis_pool_size = synthesis_pool_size
        self._counter = 0
        self.voice = None
        self.interruption_time = 0
        self.tts_host = f"https://{SPEECH_REGION}.tts.speech.microsoft.com"

    def configure(self, voice: str):
        logger.info(f"Configuring voice: {voice}")
        endpoint_id = ""
        endpoint_prefix = "tts"
        if voice.startswith("CNV:"):
            voice = voice[4:]
            endpoint_id = CNV_DEPLOYMENT_ID
            endpoint_prefix = "voice"

        self.voice = voice
        endpoint = f"wss://{SPEECH_REGION}.{endpoint_prefix}.speech.microsoft.com/cognitiveservices/websocket/v1?trafficType=RealtimePlus&enableTalkingAvatar=true"

        if SPEECH_KEY:
            self.speech_config = speechsdk.SpeechConfig(subscription=SPEECH_KEY, endpoint=endpoint)
        else:
            auth_token = f"aad#{SPEECH_RESOURCE_ID}#{token_provider()}"
            self.speech_config = speechsdk.SpeechConfig(endpoint=endpoint)
        self.speech_config.speech_synthesis_voice_name = voice
        self.speech_config.endpoint_id = endpoint_id
        self.speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
        )
        # self.speech_config.set_property(speechsdk.PropertyId.Speech_LogFilename, "speechsdk-avatar.log")
        self.speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=self.speech_config, audio_config=None)
        self.speech_synthesizer.synthesis_started.connect(
            lambda evt: logger.info(f"Synthesis started: {evt.result.reason}")
        )
        self.speech_synthesizer.synthesis_completed.connect(
            lambda evt: logger.info(f"Synthesis completed: {evt.result.reason}")
        )
        self.speech_synthesizer.synthesis_canceled.connect(
            lambda evt: logger.error(f"Synthesis canceled: {evt.result.reason}")
        )
        if not SPEECH_KEY:
            self.speech_synthesizer.authorization_token = auth_token

    def text_to_speech(
        self, voice: str, speed: str = "medium"
    ) -> Tuple[InputTextStream, AioOutputStream]:
        # input_stream = sp
        logger.info(f"Synthesizing text with voice: {voice}")
        # synthesis_request.rate = speed
        current_synthesizer = self.speech_synthesizer
        start_time = time.time()

        aio_stream = AioOutputStream()
        input_stream = InputTextStreamFromQueue()

        async def read_from_data_stream():
            inputs = []
            async for chunk in input_stream:
                inputs.append(chunk)
            contents = "".join(inputs)

            if start_time < self.interruption_time:
                logger.info("Synthesis interrupted, ignoring this turn")
                return
            
            result = current_synthesizer.start_speaking_text(contents)
            stream = speechsdk.AudioDataStream(result)
            loop = asyncio.get_running_loop()

            while True:
                if start_time < self.interruption_time:
                    logger.info("Synthesis interrupted in real time, stopping read loop.")
                    break
                chunk = bytes(2400 * 4)
                read = await loop.run_in_executor(None, stream.read_data, chunk)
                if read == 0:
                    break

                aio_stream.write_data(chunk[:read])
                
            if stream.status == speechsdk.StreamStatus.Canceled:
                logger.error(
                    f"Speech synthesis failed: {stream.status}, details: {stream.cancellation_details.error_details}"
                )
            aio_stream.end_of_stream()

        asyncio.create_task(read_from_data_stream())
        return input_stream, aio_stream

    async def interrupt(self):
        self.interruption_time = time.time()
        # todo: uncomment this when the service supports it
        try:
            connection = speechsdk.Connection.from_speech_synthesizer(self.speech_synthesizer)
            sending = connection.send_message_async('synthesis.control', '{"action":"stop"}')
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sending.get)
        except Exception as e:
            logger.error(f"Error interrupting synthesis: {e}")

    async def get_ice_servers(self):
        headers = {}
        if SPEECH_KEY:
            headers["Ocp-Apim-Subscription-Key"] = SPEECH_KEY
        else:
            headers["Authorization"] = f"Bearer aad#{SPEECH_RESOURCE_ID}#{token_provider()}"
        async with ClientSession(headers=headers) as session:
            async with session.get(f"{self.tts_host}/cognitiveservices/avatar/relay/token/v1") as response:
                response.raise_for_status()
                j = await response.text()
                return IceServer.model_validate_json(j)

    async def connect(self, client_description: str, ice_server: IceServer) -> str:
        avatar_config = {
            "synthesis": {
                "video": {
                    "protocol": {
                        "name": "WebRTC",
                        "webrtcConfig": {
                            "clientDescription": client_description,
                            "iceServers": [
                                {
                                    "urls": [ice_server.urls[0]],
                                    "username": ice_server.username,
                                    "credential": ice_server.credential,
                                }
                            ],
                        },
                    },
                    "talkingAvatar": { #need to change this for custom
                        "customized": False,
                        "character": "meg",
                        "style": "formal",
                    },
                }
            }
        }

        connection = speechsdk.Connection.from_speech_synthesizer(self.speech_synthesizer)
        connection.set_message_property("speech.config", "context", json.dumps(avatar_config))

        loop = asyncio.get_running_loop()
        speech_synthesis_result = await loop.run_in_executor(None, self.speech_synthesizer.speak_text, "")
        if speech_synthesis_result.reason == speechsdk.ResultReason.Canceled:
            cancellation_details = speech_synthesis_result.cancellation_details
            logger.error(
                f"Speech synthesis canceled: {cancellation_details.reason}, result id: {speech_synthesis_result.result_id}"
            )
            if cancellation_details.reason == speechsdk.CancellationReason.Error:
                logger.error(f"Error details: {cancellation_details.error_details}")
                raise Exception(cancellation_details.error_details)
        turn_start_message = self.speech_synthesizer.properties.get_property_by_name(
            "SpeechSDKInternal-ExtraTurnStartMessage"
        )
        if not turn_start_message:
            logger.error("No turn start message")
            raise Exception("No turn start message")
        remote_sdp = json.loads(turn_start_message).get("webrtc", {}).get("connectionString")
        if not remote_sdp:
            logger.error("No remote SDP")
            raise Exception("No remote SDP")
        return remote_sdp
        # self.speech_synthesizer.start_speaking_text_async("hello world")

    def close(self):
        del self.speech_synthesizer


if __name__ == "__main__":

    async def main():
        logging.basicConfig(level=logging.INFO)
        client = Client()
        client.configure("en-US-Andrew:DragonHDLatestNeural")
        token = await client.get_ice_servers()
        print("token", token.model_dump_json())
        # add header to the wave file

    asyncio.run(main())

# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root for full license information.

# import asyncio
# import json
# import logging
# import os
# import time
# from typing import Tuple

# import azure.cognitiveservices.speech as speechsdk
# from aiohttp import ClientSession
# from data_models import IceServer
# from azure_tts import AioOutputStream, InputTextStream, InputTextStreamFromQueue, token_provider

# logger = logging.getLogger(__name__)

# # === NEW ENV VARS ===
# # CUSTOM_TTS_ENDPOINT_URL: if set, use this exact WebSocket URL instead of constructing via region+prefix.
# # CUSTOM_TTS_VOICE_NAME: if set, this voice string will override whatever is passed into configure().
# CUSTOM_TTS_ENDPOINT_URL = os.environ.get("CUSTOM_TTS_ENDPOINT_URL", None)
# CUSTOM_TTS_VOICE_NAME = os.environ.get("CUSTOM_TTS_VOICE_NAME", None)

# SPEECH_REGION = os.environ.get("SPEECH_REGION")
# SPEECH_KEY = os.environ.get('SPEECH_KEY')
# SPEECH_RESOURCE_ID = os.environ.get("SPEECH_RESOURCE_ID")
# CNV_DEPLOYMENT_ID = os.environ.get("CNV_DEPLOYMENT_ID", "bfab6398-3c4a-4b85-8e7b-c155ca8bfa50")

# if not SPEECH_REGION:
#     raise ValueError("SPEECH_REGION must be set")

# if not (SPEECH_KEY or SPEECH_RESOURCE_ID):
#     raise ValueError("SPEECH_KEY or SPEECH_RESOURCE_ID must be set")


# class Client:
#     def __init__(self, synthesis_pool_size: int = 2):
#         if synthesis_pool_size < 1:
#             raise ValueError("synthesis_pool_size must be at least 1")
#         self.synthesis_pool_size = synthesis_pool_size
#         self._counter = 0
#         self.voice = None
#         self.interruption_time = 0
#         # Default TTS host (for token fetch). This is not used for the WebSocket URL if CUSTOM_TTS_ENDPOINT_URL is set.
#         self.tts_host = f"https://{SPEECH_REGION}.tts.speech.microsoft.com"

#     def configure(self, voice: str):
#         """
#         Configure the Speech SDK for "talking avatar" over WebSocket.
#         If CUSTOM_TTS_VOICE_NAME is set, it overrides the voice passed in.
#         If CUSTOM_TTS_ENDPOINT_URL is set, we use that exact URL (plus query params);
#         otherwise we fall back to region-based construction.
#         """
#         # === override voice if user provided a custom‐voice env var ===
#         effective_voice = CUSTOM_TTS_VOICE_NAME or voice
#         logger.info(f"Configuring voice: {effective_voice}")

#         # Determine if this is a CNV‐style (custom neural voice) deployment:
#         endpoint_id = ""
#         endpoint_prefix = "tts"
#         if effective_voice.startswith("CNV:"):
#             # If you had a CNV:<deploymentName> prefix, we remove "CNV:" and treat the suffix
#             # as the true "voice". We also force endpoint_id to CNV_DEPLOYMENT_ID.
#             effective_voice = effective_voice[4:]
#             endpoint_id = CNV_DEPLOYMENT_ID
#             endpoint_prefix = "voice"

#         self.voice = effective_voice

#         # === BUILD WEBSOCKET ENDPOINT URL ===
#         if CUSTOM_TTS_ENDPOINT_URL:
#             # Use exactly what the user provided (they might already include "wss://.../v1")
#             # Append necessary query string for avatar usage:
#             base_ws = CUSTOM_TTS_ENDPOINT_URL
#             # If CUSTOM_TTS_ENDPOINT_URL already contains "?...", we should NOT duplicate.
#             if "?" in base_ws:
#                 endpoint = base_ws
#             else:
#                 endpoint = f"{base_ws}?trafficType=RealtimePlus&enableTalkingAvatar=true"
#         else:
#             # Fallback to: wss://{region}.{prefix}.speech.microsoft.com/cognitiveservices/websocket/v1?trafficType=RealtimePlus&enableTalkingAvatar=true
#             endpoint = (
#                 f"wss://{SPEECH_REGION}.{endpoint_prefix}.speech.microsoft.com"
#                 f"/cognitiveservices/websocket/v1?trafficType=RealtimePlus&enableTalkingAvatar=true"
#             )

#         # === CREATE SpeechConfig ===
#         if SPEECH_KEY:
#             self.speech_config = speechsdk.SpeechConfig(subscription=SPEECH_KEY, endpoint=endpoint)
#         else:
#             # If using Managed Identity or Resource ID, we need a bearer token:
#             auth_token = f"aad#{SPEECH_RESOURCE_ID}#{token_provider()}"
#             self.speech_config = speechsdk.SpeechConfig(endpoint=endpoint)
#             self.speech_synthesizer_authorization_token = auth_token

#         # Set up the voice name (this must match a deployed model on Azure)
#         self.speech_config.speech_synthesis_voice_name = effective_voice
#         # If this is a CNV‐style deployment, tell the SDK which deploymentId to use:
#         if endpoint_id:
#             self.speech_config.endpoint_id = endpoint_id

#         # We want raw PCM for the avatar pipeline:
#         self.speech_config.set_speech_synthesis_output_format(
#             speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
#         )

#         # Instantiate the SpeechSynthesizer with no local audioConfig (we stream out ourselves):
#         if SPEECH_KEY:
#             self.speech_synthesizer = speechsdk.SpeechSynthesizer(
#                 speech_config=self.speech_config, audio_config=None
#             )
#         else:
#             self.speech_synthesizer = speechsdk.SpeechSynthesizer(
#                 speech_config=self.speech_config, audio_config=None
#             )
#             # Attach token if we went down the Resource ID path:
#             self.speech_synthesizer.authorization_token = auth_token

#         # Hook up events (optional, but helpful for debugging)
#         self.speech_synthesizer.synthesis_started.connect(
#             lambda evt: logger.info(f"Synthesis started: {evt.result.reason}")
#         )
#         self.speech_synthesizer.synthesis_completed.connect(
#             lambda evt: logger.info(f"Synthesis completed: {evt.result.reason}")
#         )
#         self.speech_synthesizer.synthesis_canceled.connect(
#             lambda evt: logger.error(f"Synthesis canceled: {evt.result.reason}")
#         )

#     def text_to_speech(
#         self, voice: str, speed: str = "medium"
#     ) -> Tuple[InputTextStream, AioOutputStream]:
#         """
#         Return (InputTextStream, AioOutputStream) so that whoever calls us can push text
#         into the TTS pipeline (via InputTextStream) and read raw PCM from AioOutputStream.
#         """
#         logger.info(f"Synthesizing text with voice: {self.voice}")

#         current_synthesizer = self.speech_synthesizer
#         start_time = time.time()

#         aio_stream = AioOutputStream()
#         input_stream = InputTextStreamFromQueue()

#         async def read_from_data_stream():
#             # Collect all text chunks until close(), then issue one TTS call:
#             inputs = []
#             async for chunk in input_stream:
#                 inputs.append(chunk)
#             full_text = "".join(inputs)

#             # If interruption happened before we started:
#             if start_time < self.interruption_time:
#                 logger.info("Synthesis interrupted before start; dropping this turn.")
#                 return

#             # Kick off the TTS call. Because we're using a WebSocket connection,
#             # start_speaking_text() will send it out; we then wrap into AudioDataStream.
#             result = current_synthesizer.start_speaking_text(full_text)
#             stream = speechsdk.AudioDataStream(result)
#             loop = asyncio.get_running_loop()

#             while True:
#                 if start_time < self.interruption_time:
#                     logger.info("Synthesis interrupted mid‐stream; stopping read loop.")
#                     break

#                 # Read up to 2400 samples (×4 bytes) at a time:
#                 chunk = bytes(2400 * 4)
#                 read = await loop.run_in_executor(None, stream.read_data, chunk)
#                 if read == 0:
#                     break

#                 aio_stream.write_data(chunk[:read])

#             if stream.status == speechsdk.StreamStatus.Canceled:
#                 logger.error(
#                     f"Speech synthesis failed: {stream.status}, details: {stream.cancellation_details.error_details}"
#                 )

#             aio_stream.end_of_stream()

#         # Launch the read loop in the background:
#         asyncio.create_task(read_from_data_stream())
#         return input_stream, aio_stream

#     async def interrupt(self):
#         """
#         If the user interrupts (e.g. starts talking again), we need to tell the TTS to stop.
#         """
#         self.interruption_time = time.time()
#         try:
#             connection = speechsdk.Connection.from_speech_synthesizer(self.speech_synthesizer)
#             sending = connection.send_message_async('synthesis.control', '{"action":"stop"}')
#             loop = asyncio.get_running_loop()
#             await loop.run_in_executor(None, sending.get)
#         except Exception as e:
#             logger.error(f"Error interrupting synthesis: {e}")

#     async def get_ice_servers(self):
#         """
#         Fetch the TURN/STUN relay token for WebRTC. This is required for talking avatars.
#         """
#         headers = {}
#         if SPEECH_KEY:
#             headers["Ocp-Apim-Subscription-Key"] = SPEECH_KEY
#         else:
#             # If no key, we fetch a Bearer token:
#             headers["Authorization"] = f"Bearer aad#{SPEECH_RESOURCE_ID}#{token_provider()}"

#         async with ClientSession(headers=headers) as session:
#             async with session.get(f"{self.tts_host}/cognitiveservices/avatar/relay/token/v1") as response:
#                 response.raise_for_status()
#                 j = await response.text()
#                 return IceServer.model_validate_json(j)

#     async def connect(self, client_description: str, ice_server: IceServer) -> str:
#         """
#         This is called when the front‐end says “extension.avatar.connect” and hands us a
#         local SDP offer. We need to send Azure’s avatar service a JSON blob that describes
#         our WebRTC client (including ICE servers), plus “talkingAvatar.character” info.
#         Then we call speak_text("") just to push that JSON out over the WebSocket, which
#         prompts Azure to respond with our remote SDP.
#         """
#         avatar_config = {
#             "synthesis": {
#                 "video": {
#                     "protocol": {
#                         "name": "WebRTC",
#                         "webrtcConfig": {
#                             "clientDescription": client_description,
#                             "iceServers": [
#                                 {
#                                     "urls": [ice_server.urls[0]],
#                                     "username": ice_server.username,
#                                     "credential": ice_server.credential,
#                                 }
#                             ],
#                         },
#                     },
#                     "talkingAvatar": {
#                         "customized": True,
#                         # The “character” here must match your custom avatar deployment.
#                         # In most samples, “meg” is the default. You may need to swap to your
#                         # own character ID if you had a custom model. For now, we leave “meg.”
#                         "character": "meg",
#                         "style": "formal",
#                     },
#                 }
#             }
#         }

#         connection = speechsdk.Connection.from_speech_synthesizer(self.speech_synthesizer)
#         connection.set_message_property("speech.config", "context", json.dumps(avatar_config))

#         # Kick off a zero‐text speak_text(). That forces the avatar service to handshake.
#         loop = asyncio.get_running_loop()
#         speech_synthesis_result = await loop.run_in_executor(None, self.speech_synthesizer.speak_text, "")
#         if speech_synthesis_result.reason == speechsdk.ResultReason.Canceled:
#             cancellation_details = speech_synthesis_result.cancellation_details
#             logger.error(
#                 f"Speech synthesis canceled: {cancellation_details.reason}, result id: {speech_synthesis_result.result_id}"
#             )
#             if cancellation_details.reason == speechsdk.CancellationReason.Error:
#                 logger.error(f"Error details: {cancellation_details.error_details}")
#                 raise Exception(cancellation_details.error_details)

#         turn_start_message = self.speech_synthesizer.properties.get_property_by_name(
#             "SpeechSDKInternal-ExtraTurnStartMessage"
#         )
#         if not turn_start_message:
#             logger.error("No turn start message")
#             raise Exception("No turn start message")

#         remote_sdp = json.loads(turn_start_message).get("webrtc", {}).get("connectionString")
#         if not remote_sdp:
#             logger.error("No remote SDP")
#             raise Exception("No remote SDP")
#         return remote_sdp

#     def close(self):
#         """
#         Tear down the synthesizer to free resources.
#         """
#         del self.speech_synthesizer


# if __name__ == "__main__":
#     # Quick test / debug harness:
#     async def main():
#         logging.basicConfig(level=logging.INFO)
#         client = Client()
#         # If you want to hard‐wire the voice, set CUSTOM_TTS_VOICE_NAME above:
#         client.configure("en-US-Andrew:DragonHDLatestNeural")
#         token = await client.get_ice_servers()
#         print("ICE token", token.model_dump_json())
#     asyncio.run(main())
