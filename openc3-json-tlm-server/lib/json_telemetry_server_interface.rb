# encoding: ascii-8bit

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

require 'openc3/interfaces/http_server_interface'
require 'openc3/interfaces/interface'
require 'json'
require 'webrick'

module OpenC3
  # Accepts POST /<target_name>/<packet_name> with a flat JSON body and maps
  # JSON keys to packet items by name (case-insensitive). No HTTP-specific
  # fields required in packet definitions.
  class JsonTelemetryServerInterface < HttpServerInterface
    def initialize(port = 8765)
      super(port)
      @api_key = nil
    end

    # Supported Options:
    # API_KEY       - Bearer token required in Authorization header
    # LISTEN_ADDRESS - inherited from HttpServerInterface
    def set_option(option_name, option_values)
      super(option_name, option_values)
      if option_name.upcase == 'API_KEY'
        @api_key = option_values[0]
      end
    end

    def connect
      @server = WEBrick::HTTPServer.new(
        :BindAddress => @listen_address,
        :Port => @port,
        :Logger => WEBrick::Log.new('/dev/null'),
        :AccessLog => []
      )
      @request_queue = Queue.new

      api_key = @api_key
      request_queue = @request_queue

      @server.mount_proc '/' do |req, res|
        handle_request(req, res, api_key, request_queue)
      end

      Interface.instance_method(:connect).bind(self).call

      Thread.new do
        @server.start
      end
    end

    def convert_data_to_packet(data, extra = nil)
      unless extra
        return Packet.new(nil, nil, :BIG_ENDIAN, nil, data.to_s)
      end

      target_name = extra['HTTP_REQUEST_TARGET_NAME']
      packet_name = extra['HTTP_REQUEST_PACKET_NAME']

      unless target_name && packet_name
        return Packet.new(nil, nil, :BIG_ENDIAN, nil, data.to_s)
      end

      begin
        packet = System.telemetry.packet(target_name, packet_name)
        packet = packet.clone
      rescue
        return Packet.new(nil, nil, :BIG_ENDIAN, nil, data.to_s)
      end

      begin
        json_data = JSON.parse(data.to_s)
        map_json_to_packet(json_data, packet)
      rescue JSON::ParserError
        # already rejected upstream; return packet with defaults
      end

      packet
    end

    private

    def map_json_to_packet(json_data, packet)
      item_lookup = {}
      packet.items.each do |name, item|
        item_lookup[item.key.upcase] = name
      end
      json_data.each do |key, value|
        item_name = item_lookup[key.upcase]
        packet.write(item_name, value) if item_name
      end
    end

    def handle_request(req, res, api_key, request_queue)
      unless req.request_method == 'POST'
        res.status = 405
        res['Allow'] = 'POST'
        res.body = ''
        return
      end

      if api_key
        auth = req['Authorization'] || ''
        token = auth.start_with?('Bearer ') ? auth[7..] : nil
        unless token == api_key
          res.status = 401
          res['WWW-Authenticate'] = 'Bearer'
          res.body = ''
          return
        end
      end

      parts = req.path.split('/').reject(&:empty?)
      unless parts.length == 2
        res.status = 404
        res.body = ''
        return
      end

      target_name = parts[0].upcase
      packet_name = parts[1].upcase

      begin
        System.telemetry.packet(target_name, packet_name)
      rescue
        res.status = 404
        res.body = ''
        return
      end

      body = req.body.to_s
      begin
        JSON.parse(body)
      rescue JSON::ParserError
        res.status = 400
        res.body = ''
        return
      end

      extra = {
        'HTTP_REQUEST_TARGET_NAME' => target_name,
        'HTTP_REQUEST_PACKET_NAME' => packet_name,
      }
      request_queue << [body.dup, extra]

      res.status = 200
      res.body = ''
    end
  end
end
