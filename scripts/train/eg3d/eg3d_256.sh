export CUDA_VISIBLE_DEVICES=5
python train.py \
    --config-name eg3d \
    data.dataset.preload_image=False \
    precache_rays=False \
    renderer.tri_plane_sizes=[256] \
    visualization.visdom_env='eg3d_256_density_no_sig' \
    checkpoint_epoch_interval=10



    
