
### Installation

1. git clone NeMo
2. git checkout 6f7077e9fbf1d966e49b4c0870e0d915d06efd06
3. install NeMo following NeMo instruction using ./reinstall.sh
4. Download librispeech alignments from https://drive.google.com/file/d/1WYfgr31T-PPwMcxuAq09XZfHQO5Mw8fE/view?usp=sharing5
5. pip install pyroomacoustics==0.7.0
5. check paths in `librispeech_sim_clean.sh` and change them. 
6. run `librispeech_sim_clean.sh`

Note you may want to remove my conda environment lines in `librispeech_sim_clean.sh` 
and put yours. 


### TODOs

1. use scaper for noise
   2. use audio continuation to augment noise clips to short length of e.g. 10 seconds then mix them together using scaper.
   3. lhotse manifest --> extract noise --> save clips 
   4. clips short than x --> audio continuation --> save 
   5. clips more than x --> symlink 
   6. parse audio list 
   7. construct scaper

need also to 
    