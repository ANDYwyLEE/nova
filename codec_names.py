# codec_names.py
# FFmpeg/PyAV에서 나오는 raw codec_name → 사용자 친화적 이름 매핑
# IP 카메라/RTSP 스트림에서 자주 나오는 코덱 위주로 정리
# 출처: FFmpeg codec list + 실제 카메라 경험

CODEC_FRIENDLY_NAMES = {
    # 가장 흔한 현대 코덱
    'h264': 'H.264 (AVC)',
    'hevc': 'H.265 (HEVC)',
    'av1': 'AV1',

    # MJPEG 계열 (저가/오래된 카메라 서브스트림)
    'mjpeg': 'MJPEG (Motion JPEG)',
    'jpeg': 'JPEG',

    # 구형 코덱 (중국산 오래된 모델)
    'mpeg4': 'MPEG-4 Visual (Part 2)',
    'mpeg2video': 'MPEG-2 Video',
    'h263': 'H.263',
    'h263p': 'H.263+',
    'flv1': 'FLV1 (Sorenson Spark)',
    'svq3': 'Sorenson Video 3 (SVQ3)',

    # Microsoft/Windows 계열
    'vc1': 'VC-1',
    'wmv1': 'WMV1',
    'wmv2': 'WMV2',
    'wmv3': 'WMV3 (WMV9)',

    # Google/WebM 계열
    'vp8': 'VP8',
    'vp9': 'VP9',

    # 기타 드문 코덱
    'rv10': 'RealVideo 1.0',
    'rv20': 'RealVideo 2.0',
    'rv30': 'RealVideo 3.0',
    'rv40': 'RealVideo 4.0',
    'snow': 'Snow (Wavelet)',
    'theora': 'Theora',
    'prores': 'Apple ProRes',
    'dnxhd': 'Avid DNxHD',
    'cinepak': 'Cinepak',
    'msvideo1': 'Microsoft Video 1',

    # 오디오 코덱 (비디오 스트림에 섞여 나올 수 있음)
    'aac': 'AAC (Audio)',
    'mp3': 'MP3 (Audio)',
    'ac3': 'AC-3 (Dolby Digital Audio)',
    'eac3': 'E-AC-3 (Enhanced AC-3)',
    'pcm_alaw': 'G.711 A-law (Audio)',
    'pcm_mulaw': 'G.711 μ-law (Audio)',
    'opus': 'Opus (Audio)',
    'vorbis': 'Vorbis (Audio)',
}

def get_friendly_codec_name(raw_name: str) -> str:
    """
    raw codec_name을 사용자 친화적인 이름으로 변환
    모르면 대문자로 반환
    """
    if not raw_name:
        return "Unknown"
    return CODEC_FRIENDLY_NAMES.get(raw_name.lower(), raw_name.upper())
