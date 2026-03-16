class Shard < Formula
  include Language::Python::Virtualenv

  desc "TDD-driven, parallelized AI coding orchestrator"
  homepage "https://github.com/nihalgunu/Shard"
  url "https://github.com/nihalgunu/Shard/archive/refs/tags/v1.0.1.tar.gz"
  sha256 "da221b6d81457fa8a8675d1190296e8ab1969e9f2a6fe9c6cccf08a1758e44fc"
  license "Apache-2.0"

  depends_on "python@3.11"

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/shard", "--version"
  end
end
