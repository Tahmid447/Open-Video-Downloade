"""Validate completed containers before publication; never trust an extension alone."""
import asyncio
import json
import math
from pathlib import Path


async def probe(path):
    process = await asyncio.create_subprocess_exec('ffprobe','-protocol_whitelist','file','-v','error','-show_format','-show_streams','-of','json',str(Path(path)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024*1024)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 20)
        if process.returncode or len(stdout) > 1024*1024:
            raise RuntimeError('Completed media failed FFprobe validation.')
        data = json.loads(stdout)
        streams = [s for s in data.get('streams',[]) if s.get('codec_type') in {'video','audio'}]
        duration = float(data.get('format',{}).get('duration') or 0)
        if not streams or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError('Completed media has no playable streams or duration.')
        return dict(duration=duration, size=Path(path).stat().st_size,
                    streams=[{k:s.get(k) for k in ('codec_type','codec_name','width','height','sample_rate')} for s in streams])
    except (ValueError, asyncio.TimeoutError) as exc:
        raise RuntimeError('Completed media could not be validated.') from exc
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
