#!/bin/bash

source ../../../../../miniconda3/bin/activate
conda activate nemold

stage=3
NUM_WORKERS=16
NEMO_ROOT=/ocean/projects/cis210027p/scornell/2speakers_convs_data/nemo/NeMo/
LIBRISPEECH_ROOT=/ocean/projects/cis210027p/shared/corpora/librispeech/
LIBRISPEECH_ALIGN_PATH=/ocean/projects/cis210027p/scornell/2speakers_convs_data/nemo/NeMo/tools/speech_data_simulator/librispeech_alignments/LibriSpeech
OUTPUT_DIR=./nemo_sim_2spk_noisy

mkdir -p $OUTPUT_DIR/librispeech_manifests

if [ ${stage} -le 1 ]; then
  python $NEMO_ROOT/scripts/dataset_processing/get_librispeech_data.py --data_root $LIBRISPEECH_ROOT \
    --data_sets ALL \
    --manifest_path $OUTPUT_DIR/librispeech_manifests \
    --skip_download
fi


if [ ${stage} -le 2 ]; then
for SPLIT in train-clean-100 train-clean-360 train-other-500; do

python $NEMO_ROOT/scripts/speaker_tasks/create_alignment_manifest.py \
  --input_manifest_filepath $OUTPUT_DIR/librispeech_manifests/$SPLIT.json \
  --base_alignment_path $LIBRISPEECH_ALIGN_PATH \
  --output_manifest_filepath $OUTPUT_DIR/$SPLIT-align.json \
  --ctm_output_directory $OUTPUT_DIR/ctm_out \
  --libri_dataset_split $SPLIT
done
fi

# merge all manifests for librispeech
if [ ${stage} -le 4 ]; then
for input_file in train-clean-100 train-clean-360; do
  # Append each input file to the output file
  cat "${OUTPUT_DIR}/${input_file}-align.json" >> "${OUTPUT_DIR}/all-align.json"
done
fi

if [ ${stage} -le 4 ]; then
python multispeaker_simulator.py --config-path='conf' --config-name='2_spkdata_simulator.yaml' \
  num_workers=$NUM_WORKERS \
  data_simulator.random_seed=777 \
  data_simulator.manifest_filepath=$OUTPUT_DIR/all-align.json \
  data_simulator.outputs.output_dir=$OUTPUT_DIR/simulated_data \
  data_simulator.background_noise.add_bg=False \
  data_simulator.rir_generation.use_rir=True
fi





