import lhotse
import glob
from pathlib import Path


import glob
import json
import os
from pathlib import Path

import lhotse
import lhotse.supervision
import soundfile as sf


delta = 0.0
root_folder = "/ocean/projects/cis210027p/scornell/2speakers_convs_data/nemo/NeMo/tools/speech_data_simulator/nemo_sim_2spk_noisy/simulated_data"
dset_name="nemo2spkrev"

def ctm2utterances(ctm_spk_align, delta=0.0):
    # first: we sort by starting time
    ctm_spk_align = sorted(ctm_spk_align, key=lambda x: x["start"])

    out = []
    stack = []
    # fix_count = 0
    for word in ctm_spk_align:
        if len(stack) == 0:
            stack.append(word)
            continue
        # else, elem in the stack
        c_delta = word["start"] - stack[-1]["stop"]
        if c_delta <= -0.1:
            # fix_count += 1
            duration = word["stop"] - word["start"]
            word["start"] = stack[-1]["stop"]
            word["stop"] = word["start"] + duration

        c_delta = word["start"] - stack[-1]["stop"]
        if c_delta <= delta:
            stack.append(word)
        else:
            out.append(stack)  # append utterance to stack
            stack = []  # stack empty again
    # print(fix_count)
    return out


def parse_ctm(ctm_file, split_over=0.0):
    with open(ctm_file, "r") as f:
        lines = f.readlines()

    spk2words = {}
    for l in lines:
        l = l.rstrip("\n").split(" ")

        source, channel, start, duration, word, _ = l[:6]
        spkid = l[-1]
        start = float(start)
        duration = float(duration)
        if not spkid in spk2words.keys():
            spk2words[spkid] = []
        spk2words[spkid].append(
            {
                "source": source,
                "spk": spkid,
                "start": start,
                "stop": start + duration,
                "word": word,
                "channel": channel,
            }
        )

    # here now we split
    out = []
    for c_spk in spk2words.keys():
        out.extend(ctm2utterances(spk2words[c_spk], split_over))
    return out  # list ready for lhotse


def parse_supervisions(ctm_file, delta=0.0):
    parsed = parse_ctm(ctm_file, delta)

    to_supervision = []
    for utt in parsed:
        sess_id = utt[0]["source"].split("_")[-1]
        start = utt[0]["start"]
        stop = utt[-1]["stop"]
        speaker = utt[0]["spk"]
        c_id = f"{dset_name}-{sess_id}-{speaker}-{round(start, 3)}-{round(stop, 3)}"
        c_duration = stop - start

        text = " ".join([x["word"] for x in utt])
        c_sup = lhotse.supervision.SupervisionSegment(
            id=c_id,
            recording_id=f"{dset_name}-{sess_id}",
            speaker=f"{speaker}",
            start=utt[0]["start"],
            duration=c_duration,
            text=text,
        )
        to_supervision.append(c_sup)
    return to_supervision


def parse_nemo_ds(dset_folder, dset_name=None, delta=0.0):
    root_folder = str(Path(dset_folder).parent)
    if dset_name is None:
        dset_name = str(Path(root_folder).parent)

    recordings = glob.glob(os.path.join(root_folder, "*.wav"))
    # create recording manifest
    rec_manifest = []
    sup_manifest = []
    for c_rec in recordings:
        filename = Path(c_rec).stem
        sess_id = filename.split("_")[-1]
        infos = sf.SoundFile(c_rec)
        c_rec_sup = lhotse.Recording(
            id=f"{dset_name}-{sess_id}",
            sources=[
                lhotse.AudioSource(
                    f"{dset_name}-{sess_id}",
                    [x for x in range(0, infos.channels)],
                    c_rec,
                )
            ],
            sampling_rate=infos.samplerate,
            num_samples=len(infos),
            duration=infos.frames / infos.samplerate,
            channel_ids=[x for x in range(0, infos.channels)],
        )
        rec_manifest.append(c_rec_sup)
        # parse supervisions here
        c_ctm_file = os.path.join(Path(c_rec).parent, Path(c_rec).stem + ".ctm")
        c_sup = parse_supervisions(c_ctm_file, delta)
        sup_manifest.extend(c_sup)

    recording_set, supervision_set = lhotse.fix_manifests(
        lhotse.RecordingSet.from_recordings(rec_manifest),
        lhotse.SupervisionSet.from_segments(sup_manifest),
    )
    lhotse.validate_recordings_and_supervisions(recording_set, supervision_set)
    supervision_set.to_file(
        os.path.join(root_folder, dset_name, f"{dset_name}_supervisions.jsonl.gz")
    )
    recording_set.to_file(
        os.path.join(root_folder, dset_name, f"{dset_name}_recordings.jsonl.gz")
    )



out_folder_manifest = os.path.join(root_folder, dset_name)
os.makedirs(out_folder_manifest, exist_ok=True)
parse_nemo_ds(os.path.join(root_folder, dset_name), dset_name, delta)