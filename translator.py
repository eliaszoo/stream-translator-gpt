import argparse
import os
import requests
import signal
import sys
import subprocess
import tempfile
import threading
import grpc
import transcribe_pb2
import transcribe_pb2_grpc
from datetime import datetime
from concurrent import futures
from scipy.io.wavfile import write as write_audio

import ffmpeg
import numpy as np
from whisper.audio import SAMPLE_RATE

import filters
from openai_api import TranslationTask, ParallelTranslator, SerialTranslator, whisper_transcribe
from vad import VAD


class RingBuffer:

    def __init__(self, size):
        self.size = size
        self.data = []
        self.full = False
        self.cur = 0

    def append(self, x):
        if self.size <= 0:
            return
        if self.full:
            self.data[self.cur] = x
            self.cur = (self.cur + 1) % self.size
        else:
            self.data.append(x)
            if len(self.data) == self.size:
                self.full = True

    def get_all(self):
        """ Get all elements in chronological order from oldest to newest. """
        all_data = []
        for i in range(len(self.data)):
            idx = (i + self.cur) % self.size
            all_data.append(self.data[idx])
        return all_data

    def has_repetition(self):
        prev = None
        for elem in self.data:
            if elem == prev:
                return True
            prev = elem
        return False

    def clear(self):
        self.data = []
        self.full = False
        self.cur = 0

class StreamSlicer:

    def __init__(self, frame_duration, continuous_no_speech_threshold, min_audio_length,
                 max_audio_length, prefix_retention_length, vad_threshold, sampling_rate):
        self.vad = VAD()
        self.continuous_no_speech_threshold = round(continuous_no_speech_threshold / frame_duration)
        self.min_audio_length = round(min_audio_length / frame_duration)
        self.max_audio_length = round(max_audio_length / frame_duration)
        self.prefix_retention_length = round(prefix_retention_length / frame_duration)
        self.vad_threshold = vad_threshold
        self.sampling_rate = sampling_rate
        self.audio_buffer = []
        self.prefix_audio_buffer = []
        self.speech_count = 0
        self.no_speech_count = 0
        self.continuous_no_speech_count = 0
        self.frame_duration = frame_duration
        self.counter = 0
        self.last_slice_second = 0.0

    def put(self, audio):
        self.counter += 1
        if self.vad.is_speech(audio, self.vad_threshold, self.sampling_rate):
            self.audio_buffer.append(audio)
            self.speech_count += 1
            self.continuous_no_speech_count = 0
        else:
            #if self.speech_count == 0 and self.no_speech_count == 1:
                #self.slice()
            self.audio_buffer.append(audio)
            self.no_speech_count += 1
            self.continuous_no_speech_count += 1
        if self.speech_count and self.no_speech_count / 4 > self.speech_count:
            self.slice()
        #print(self.counter, self.speech_count, self.no_speech_count, len(self.audio_buffer))

    def should_slice(self):
        audio_len = len(self.audio_buffer)
        #print(audio_len)
        if audio_len < self.min_audio_length:
            return False
        if audio_len > self.max_audio_length:
            return True
        if self.continuous_no_speech_count >= self.continuous_no_speech_threshold:
            return True
        return False

    def slice(self):
        concatenate_buffer = self.prefix_audio_buffer + self.audio_buffer
        concatenate_audio = np.concatenate(concatenate_buffer)
        self.audio_buffer = []
        self.prefix_audio_buffer = concatenate_buffer[-self.prefix_retention_length:]
        self.speech_count = 0
        self.no_speech_count = 0
        self.continuous_no_speech_count = 0
        # self.vad.reset_states()
        slice_second = self.counter * self.frame_duration
        last_slice_second = self.last_slice_second
        self.last_slice_second = slice_second
        return concatenate_audio, (last_slice_second, slice_second)

stream_slicer = StreamSlicer(frame_duration=0.01, continuous_no_speech_threshold=0.8,
                             min_audio_length=3.0, max_audio_length=30.0,
                             prefix_retention_length=0.8, vad_threshold=0.5,
                             sampling_rate=SAMPLE_RATE)

history_audio_buffer = RingBuffer(1)
history_text_buffer = RingBuffer(0)
gmodel = ""
gfaster_whisper_args = False
glanguage = "en"
gdecode_options = {}
gwhisper_filters = []
goutput_timestamps = False
buff = np.array([])

class TranscribeService(transcribe_pb2_grpc.TranscriberServicer):
    def Trans(self, request, context):
        #print("------------------------------")
        print(request)
        #print("Received audio data", len(audio_data.data))
        # audio = np.frombuffer(audio_data.data, np.int16).flatten().astype(np.float32) / 32768.0
        audio_data = request.data
        global buff
        print("audio data",audio_data)
        #print("audio",audio)
        buff = np.concatenate((buff, audio_data))
        #print("buffer len", len(buff))
        if len(buff) >= 1600:
            #print(buff)
            #print('put')
            stream_slicer.put(buff)
            if stream_slicer.should_slice():
                # Decode the audio
                sliced_audio, time_range = stream_slicer.slice()
                print("Sliced audio", len(sliced_audio), time_range)
                history_audio_buffer.append(sliced_audio)
                clear_buffers = False
                if gfaster_whisper_args:
                    segments, info = gmodel.transcribe(sliced_audio, language=glanguage, **gdecode_options)
                    decoded_text = ""
                    previous_segment = ""
                    for segment in segments:
                        if segment.text != previous_segment:
                            decoded_text += segment.text
                            previous_segment = segment.text

                    new_prefix = decoded_text

                else:
                    print(np.concatenate(history_audio_buffer.get_all()))
                    print("".join(history_text_buffer.get_all()))
                    print(**gdecode_options)
                    result = gmodel.transcribe(np.concatenate(history_audio_buffer.get_all()),
                                            prefix="".join(history_text_buffer.get_all()),
                                            language=glanguage,
                                            without_timestamps=True,
                                            **gdecode_options)

                    print(result)
                    decoded_text = result.get("text")
                    new_prefix = ""
                    for segment in result["segments"]:
                        if segment["temperature"] < 0.5 and segment["no_speech_prob"] < 0.6:
                            new_prefix += segment["text"]
                        else:
                            # Clear history if the translation is unreliable, otherwise prompting on this leads to
                            # repetition and getting stuck.
                            clear_buffers = True

                history_text_buffer.append(new_prefix)

                if clear_buffers or history_text_buffer.has_repetition():
                    history_audio_buffer.clear()
                    history_text_buffer.clear()

                decoded_text = filter_text(decoded_text, gwhisper_filters)
                if decoded_text.strip():
                    timestamp_text = '{}-{} '.format(sec2str(time_range[0]), sec2str(
                        time_range[1])) if goutput_timestamps else ''
                    print('{}{}'.format(timestamp_text, decoded_text))
                    #yield transcribe_pb2.Text(content=decoded_text)
                else:
                    print('skip...')
                    #yield transcribe_pb2.Text(content="skip...")
            buff= np.array([])

        return transcribe_pb2_grpc.Text(content="")


def open_stream(stream, direct_url, format, cookies):
    if direct_url:
        try:
            process = (ffmpeg.input(
                stream, loglevel="panic").output("pipe:",
                                                 format="s16le",
                                                 acodec="pcm_s16le",
                                                 ac=1,
                                                 ar=SAMPLE_RATE).run_async(pipe_stdout=True))
        except ffmpeg.Error as e:
            raise RuntimeError(f"Failed to load audio: {e.stderr.decode()}") from e

        return process, None

    def writer(ytdlp_proc, ffmpeg_proc):
        while (ytdlp_proc.poll() is None) and (ffmpeg_proc.poll() is None):
            try:
                chunk = ytdlp_proc.stdout.read(1024)
                ffmpeg_proc.stdin.write(chunk)
            except (BrokenPipeError, OSError):
                pass
        ytdlp_proc.kill()
        ffmpeg_proc.kill()

    cmd = ['yt-dlp', stream, '-f', format, '-o', '-', '-q']
    if cookies:
        cmd.extend(['--cookies', cookies])
    ytdlp_process = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    try:
        ffmpeg_process = (ffmpeg.input("pipe:", loglevel="panic").output("pipe:",
                                                                         format="s16le",
                                                                         acodec="pcm_s16le",
                                                                         ac=1,
                                                                         ar=SAMPLE_RATE).run_async(
                                                                             pipe_stdin=True,
                                                                             pipe_stdout=True))
    except ffmpeg.Error as e:
        raise RuntimeError(f"Failed to load audio: {e.stderr.decode()}") from e

    thread = threading.Thread(target=writer, args=(ytdlp_process, ffmpeg_process))
    thread.start()
    return ffmpeg_process, ytdlp_process


def send_to_cqhttp(url, token, text):
    headers = {'Authorization': 'Bearer {}'.format(token)} if token else None
    data = {'message': text}
    requests.post(url, headers=headers, data=data)


def filter_text(text, whisper_filters):
    filter_name_list = whisper_filters.split(',')
    for filter_name in filter_name_list:
        filter = getattr(filters, filter_name)
        if not filter:
            raise Exception('Unknown filter: %s' % filter_name)
        text = filter(text)
    return text


def sec2str(second):
    dt = datetime.utcfromtimestamp(second)
    return dt.strftime('%H:%M:%S')


def main(url, format, direct_url, cookies, frame_duration, continuous_no_speech_threshold,
         min_audio_length, max_audio_length, prefix_retention_length, vad_threshold, model,
         language, faster_whisper_args, use_whisper_api, whisper_filters, output_timestamps,
         history_buffer_size, gpt_translation_prompt, gpt_translation_history_size, openai_api_key,
         gpt_model, gpt_translation_timeout, cqhttp_url, cqhttp_token, **decode_options):

    global stream_slicer, history_audio_buffer, history_text_buffer, gmodel, gfaster_whisper_args 
    global glanguage, gwhisper_filters, goutput_timestamps, gdecode_options

    gfaster_whisper_args = faster_whisper_args
    glanguage = language
    gwhisper_filters = whisper_filters
    goutput_timestamps = output_timestamps
    gdecode_options = decode_options


    history_audio_buffer = RingBuffer(history_buffer_size + 1)
    history_text_buffer = RingBuffer(history_buffer_size)
    stream_slicer = StreamSlicer(frame_duration=frame_duration,
                                 continuous_no_speech_threshold=continuous_no_speech_threshold,
                                 min_audio_length=min_audio_length,
                                 max_audio_length=max_audio_length,
                                 prefix_retention_length=prefix_retention_length,
                                 vad_threshold=vad_threshold,
                                 sampling_rate=SAMPLE_RATE)

    
    if faster_whisper_args:
        print("Loading faster whisper model: {}".format(faster_whisper_args["model_path"]))
        from faster_whisper import WhisperModel
        gmodel = WhisperModel(faster_whisper_args["model_path"],
                             device=faster_whisper_args["device"],
                             compute_type=faster_whisper_args["compute_type"])
    elif not use_whisper_api:
        print("Loading whisper model: {}".format(model))
        import whisper
        gmodel = whisper.load_model(model)

    translator = None
    if gpt_translation_prompt and openai_api_key:
        if gpt_translation_history_size == 0:
            translator = ParallelTranslator(openai_api_key=openai_api_key,
                                            prompt=gpt_translation_prompt,
                                            model=gpt_model,
                                            timeout=gpt_translation_timeout)
        else:
            translator = SerialTranslator(openai_api_key=openai_api_key,
                                          prompt=gpt_translation_prompt,
                                          model=gpt_model,
                                          timeout=gpt_translation_timeout,
                                          history_size=gpt_translation_history_size)

    print("Begin...")


def cli():
    parser = argparse.ArgumentParser(description="Parameters for translator.py")
    parser.add_argument('URL',
                        type=str,
                        help='Stream website and channel name, e.g. twitch.tv/forsen')
    parser.add_argument('--format',
                        type=str,
                        default='wa*',
                        help='Stream format code, '
                        'this parameter will be passed directly to yt-dlp.')
    parser.add_argument('--direct_url',
                        action='store_true',
                        help='Set this flag to pass the URL directly to ffmpeg. '
                        'Otherwise, yt-dlp is used to obtain the stream URL.')
    parser.add_argument('--cookies',
                        type=str,
                        default=None,
                        help='Used to open member-only stream, '
                        'this parameter will be passed directly to yt-dlp.')
    parser.add_argument('--frame_duration',
                        type=float,
                        default=0.1,
                        help='The unit that processes live streaming data in seconds.')
    parser.add_argument('--continuous_no_speech_threshold',
                        type=float,
                        default=0.8,
                        help='Slice if there is no speech for a continuous period in second.')
    parser.add_argument('--min_audio_length',
                        type=float,
                        default=3.0,
                        help='Minimum slice audio length in seconds.')
    parser.add_argument('--max_audio_length',
                        type=float,
                        default=30.0,
                        help='Maximum slice audio length in seconds.')
    parser.add_argument('--prefix_retention_length',
                        type=float,
                        default=0.8,
                        help='The length of the retention prefix audio during slicing.')
    parser.add_argument('--vad_threshold',
                        type=float,
                        default=0.5,
                        help='The threshold of Voice activity detection.'
                        'if the speech probability of a frame is higher than this value, '
                        'then this frame is speech.')
    parser.add_argument(
        '--model',
        type=str,
        choices=['tiny', 'tiny.en', 'small', 'small.en', 'medium', 'medium.en', 'large'],
        default='small',
        help='Model to be used for generating audio transcription. '
        'Smaller models are faster and use less VRAM, '
        'but are also less accurate. .en models are more accurate '
        'but only work on English audio.')
    parser.add_argument(
        '--task',
        type=str,
        choices=['transcribe', 'translate'],
        default='transcribe',
        help='Whether to transcribe the audio (keep original language) or translate to English.')
    parser.add_argument('--language',
                        type=str,
                        default='auto',
                        help='Language spoken in the stream. '
                        'Default option is to auto detect the spoken language. '
                        'See https://github.com/openai/whisper for available languages.')
    parser.add_argument('--history_buffer_size',
                        type=int,
                        default=0,
                        help='Times of previous audio/text to use for conditioning the model. '
                        'Set to 0 to just use audio from the last processing. '
                        'Note that this can easily lead to repetition/loops if the chosen '
                        'language/model settings do not produce good results to begin with.')
    parser.add_argument('--beam_size',
                        type=int,
                        default=5,
                        help='Number of beams in beam search. '
                        'Set to 0 to use greedy algorithm instead.')
    parser.add_argument('--best_of',
                        type=int,
                        default=5,
                        help='Number of candidates when sampling with non-zero temperature.')
    parser.add_argument('--use_faster_whisper',
                        action='store_true',
                        help='Set this flag to use faster-whisper implementation instead of '
                        'the original OpenAI implementation.')
    parser.add_argument('--faster_whisper_model_path',
                        type=str,
                        default='whisper-large-v2-ct2/',
                        help='Path to a directory containing a Whisper model '
                        'in the CTranslate2 format.')
    parser.add_argument('--faster_whisper_device',
                        type=str,
                        choices=['cuda', 'cpu', 'auto'],
                        default='cuda',
                        help='Set the device to run faster-whisper on.')
    parser.add_argument('--faster_whisper_compute_type',
                        type=str,
                        choices=['int8', 'int8_float16', 'int16', 'float16'],
                        default='float16',
                        help='Set the quantization type for faster-whisper. See '
                        'https://opennmt.net/CTranslate2/quantization.html for more info.')
    parser.add_argument('--use_whisper_api',
                        action='store_true',
                        help='Set this flag to use OpenAI Whisper API instead of '
                        'the original local Whipser.')
    parser.add_argument('--whisper_filters',
                        type=str,
                        default='emoji_filter',
                        help='Filters apply to whisper results, separated by ",".')
    parser.add_argument('--output_timestamps',
                        action='store_true',
                        help='Output the timestamp of the text when outputting the text.')
    parser.add_argument('--openai_api_key',
                        type=str,
                        default=None,
                        help='OpenAI API key if using GPT translation / Whisper API.')
    parser.add_argument('--gpt_translation_prompt',
                        type=str,
                        default=None,
                        help='If set, will translate result text to target language via GPT API. '
                        'Example: \"Translate from Japanese to Chinese\"')
    parser.add_argument('--gpt_translation_history_size',
                        type=int,
                        default=0,
                        help='The number of previous messages sent when calling the GPT API. '
                        'If the history size is 0, the GPT API will be called parallelly. '
                        'If the history size > 0, the GPT API will be called serially.')
    parser.add_argument('--gpt_model',
                        type=str,
                        default="gpt-3.5-turbo",
                        help='GPT model name, gpt-3.5-turbo or gpt-4')
    parser.add_argument('--gpt_translation_timeout',
                        type=int,
                        default=15,
                        help='If the ChatGPT translation exceeds this number of seconds, '
                        'the translation will be discarded.')
    parser.add_argument('--cqhttp_url',
                        type=str,
                        default=None,
                        help='If set, will send the result text to the cqhttp server.')
    parser.add_argument('--cqhttp_token',
                        type=str,
                        default=None,
                        help='Token of cqhttp, if it is not set on the server side, '
                        'it does not need to fill in.')

    args = parser.parse_args().__dict__
    url = args.pop("URL")
    use_faster_whisper = args.pop("use_faster_whisper")
    faster_whisper_args = dict()
    faster_whisper_args["model_path"] = args.pop("faster_whisper_model_path")
    faster_whisper_args["device"] = args.pop("faster_whisper_device")
    faster_whisper_args["compute_type"] = args.pop("faster_whisper_compute_type")

    if args['model'].endswith('.en'):
        if args['model'] == 'large.en':
            print(
                "English model does not have large model, please choose from {tiny.en, small.en, medium.en}"
            )
            sys.exit(0)
        if args['language'] != 'English' and args['language'] != 'en':
            if args['language'] == 'auto':
                print("Using .en model, setting language from auto to English")
                args['language'] = 'en'
            else:
                print(
                    "English model cannot be used to detect non english language, please choose a non .en model"
                )
                sys.exit(0)

    if use_faster_whisper and args['use_whisper_api']:
        print("Cannot use Faster Whisper and Whisper API at the same time")
        sys.exit(0)

    if (args['use_whisper_api'] or args['gpt_translation_prompt']) and not args['openai_api_key']:
        print("Please fill in the OpenAI API key when enabling GPT translation or Whisper API")
        sys.exit(0)

    if args['language'] == 'auto':
        args['language'] = None

    if args['beam_size'] == 0:
        args['beam_size'] = None

    # Remove yt-dlp cache
    for file in os.listdir('./'):
        if file.startswith('--Frag'):
            os.remove(file)

    main(url, faster_whisper_args=faster_whisper_args if use_faster_whisper else None, **args)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    transcribe_pb2_grpc.add_TranscriberServicer_to_server(TranscribeService(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    print("Server started on port 50051")
    server.wait_for_termination()


def serve():

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    transcribe_pb2_grpc.add_TranscriberServicer_to_server(TranscribeService(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    print("Server started on port 50051")
    server.wait_for_termination()

if __name__ == '__main__':
    cli()