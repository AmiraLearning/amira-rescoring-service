# Define the path to your FFmpeg libraries
FFMPEG_LIB_PATH="$(brew --prefix ffmpeg)/lib"

# Define the path to the torchcodec libraries in your venv
TORCHCODEC_PATH="./.venv/lib/python3.12/site-packages/torchcodec"

# Loop through each torchcodec core library and add the RPATH
for lib in ${TORCHCODEC_PATH}/libtorchcodec_core*.dylib; do
  echo "Patching ${lib}..."
  install_name_tool -add_rpath "${FFMPEG_LIB_PATH}" "${lib}"
done

echo "Patching complete."
